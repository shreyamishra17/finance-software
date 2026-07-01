"""Server-side normalization of RAW rows harvested by the thin FIFO extension.

The Chrome extension is being refactored into a dumb, generic harvester: it only
runs the things that MUST run inside the bank page (auto-login, navigation,
pagination, raw-cell harvest) and ships RAW innerText per configured cell to the
server.  ALL parsing/normalization that used to live in content.js
(``scrapeRows`` / ``_fixYearsSequential`` / ``_normalizeDirectionFromBalance`` /
``parseDayMonthDate`` / ``parseAmount`` / ``normalizeDate`` / ``firstLine``) now
lives here so logic can iterate server-side with ZERO extension republishes.

These functions are FAITHFUL ports of the JS in chrome_extension/content.js.
``normalize_raw_rows`` produces exactly the same transaction dict shape the old
content.js sent — ``{date, description, amount, amount_type, raw_amount,
balance, pending}`` — so the rest of the ingest pipeline (categorize → store)
is unchanged.  Keep the two in sync until the old (pre-parsed) path is retired.
"""

from __future__ import annotations

import math
import re
from datetime import datetime

try:  # dateutil is a project dependency; guard so the module imports either way.
    from dateutil import parser as _dateutil_parser
except Exception:  # pragma: no cover
    _dateutil_parser = None


# ── tiny helpers (ports of content.js text()/firstLine()) ────────────────────
def _cell(raw_row: dict, key: str) -> str:
    """Raw text for a harvested cell, trimmed. The extension already grabs
    innerText, but we trim defensively (and tolerate None / non-str)."""
    return str((raw_row or {}).get(key) or "").strip()


def first_line(s: str) -> str:
    """Port of content.js firstLine(): first line of a multi-line cell."""
    return (s or "").split("\n")[0].strip()


def _num_to_str(n: float) -> str:
    """Mirror JS ``String(Number)``: integers print without a trailing
    ``.0``; fractionals keep their (trimmed) decimals."""
    if n == int(n):
        return str(int(n))
    return f"{n:.10f}".rstrip("0").rstrip(".")


def _js_parse_float(s: str):
    """Mirror JS ``parseFloat``: read the leading numeric prefix, ignoring any
    trailing junk (e.g. ``"-123.00-"`` → ``-123.0``). Returns None if no number
    is present (JS would yield NaN)."""
    m = re.match(r"[+-]?(?:\d+\.?\d*|\.\d+)", (s or "").strip())
    return float(m.group(0)) if m else None


# ── amount parse (port of content.js parseAmount) ────────────────────────────
def parse_amount(raw: str) -> str:
    """Port of content.js parseAmount: strip currency/commas/whitespace/parens,
    return the absolute value as a JS-style string ("0" when unparseable)."""
    s = re.sub(r"[₹$€£¥,\s()]", "", raw or "")
    n = _js_parse_float(s)
    if n is None:
        n = 0.0
    return _num_to_str(abs(n))


# ── date parse (ports of normalizeDate + parseDayMonthDate) ───────────────────
def normalize_date(raw: str) -> str:
    """Port of content.js normalizeDate(): "Pending"/"Processing" → "Pending";
    otherwise parse to YYYY-MM-DD (filling a missing year with the current one,
    like JS ``new Date(raw + " " + year)``); fall back to the raw string."""
    if not raw:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    if re.match(r"^(pending|processing)\b", s, re.I):
        return "Pending"
    if _dateutil_parser is not None:
        now = datetime.now()
        default = datetime(now.year, 1, 1)
        for attempt in (s, f"{s} {now.year}"):
            try:
                d = _dateutil_parser.parse(attempt, default=default)
                return d.strftime("%Y-%m-%d")
            except (ValueError, OverflowError, TypeError):
                continue
    return s


_MON = {
    "jan": 0, "feb": 1, "mar": 2, "apr": 3, "may": 4, "jun": 5,
    "jul": 6, "aug": 7, "sep": 8, "oct": 9, "nov": 10, "dec": 11,
}


def parse_day_month_date(raw: str) -> str:
    """Port of content.js parseDayMonthDate(): ICICI's side date block carries
    month + day with NO year ("29 Jun", "Dec 29", "14 Jun 14 SUN"). Pull month
    + day and infer the year from a "last 1 year" window: a month LATER than the
    current month belongs to last year. Returns "" if no month+day is found, so
    callers fall back to normalize_date()."""
    toks = [t for t in re.split(r"[\s,]+", (raw or "").strip()) if t]
    mon = None
    day = None
    for t in toks:
        key = t.lower()[:3]
        if mon is None and key in _MON:
            mon = _MON[key]
            continue
        if day is None and re.match(r"^\d{1,2}$", t):
            day = int(t)
    if mon is None or day is None or day < 1 or day > 31:
        return ""
    now = datetime.now()
    year = now.year
    if mon > (now.month - 1):  # JS now.getMonth() is 0-based; _MON is 0-based
        year -= 1
    return f"{year}-{mon + 1:02d}-{day:02d}"


def fix_years_sequential(rows: list[dict]) -> None:
    """Port of content.js _fixYearsSequential(): for banks whose date cell has
    no year, the per-row guess can't tell this-year June from last-year June.
    The MONTH is correct; only the year may be off. Walk newest→oldest: whenever
    the month goes UP from one row to the next-older one we crossed a year
    boundary going back in time → decrement the year. Mutates rows[].date."""
    if not rows:
        return
    year = None
    prev_month = None
    for r in rows:
        m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", r.get("date") or "")
        if not m:
            continue  # skip Pending / unparseable
        mon = int(m.group(2))
        if year is None:
            year = int(m.group(1))
            prev_month = mon
            continue
        if mon > prev_month:
            year -= 1
        prev_month = mon
        r["date"] = f"{year}-{m.group(2)}-{m.group(3)}"


# ── direction from running balance (port of _normalizeDirectionFromBalance) ──
def normalize_direction_from_balance(rows: list[dict]) -> None:
    """Port of content.js _normalizeDirectionFromBalance(): the truth-tier
    signal. If the running balance went UP, money came IN (credit); if DOWN,
    money went OUT (debit). Trusts the delta ONLY when |delta| matches |amount|
    within 0.01 (banks reorder same-day rows, so adjacent display rows aren't
    always adjacent in time). Mutates rows[].amount_type."""
    if not rows or len(rows) < 2:
        return

    parsed_balance = []
    for r in rows:
        s = re.sub(r"[$,₹€£¥\s]", "", r.get("balance") or "")
        s = s.replace("(", "-").replace(")", "-")
        n = _js_parse_float(s)
        parsed_balance.append(n if (n is not None and math.isfinite(n)) else None)

    def _date_at(i):
        s = (rows[i].get("date") or "")[:10]
        try:
            return datetime.fromisoformat(s)
        except (ValueError, TypeError):
            return None

    newest_first = True
    for i in range(min(len(rows) - 1, 10)):
        a, b = _date_at(i), _date_at(i + 1)
        if a and b and a != b:
            newest_first = a > b
            break

    for i in range(len(rows) - 1):
        newer_idx = i if newest_first else i + 1
        older_idx = i + 1 if newest_first else i
        newer_bal = parsed_balance[newer_idx]
        older_bal = parsed_balance[older_idx]
        if newer_bal is None or older_bal is None:
            continue
        delta = newer_bal - older_bal
        if abs(delta) < 0.01:
            continue
        try:
            amt = float(rows[newer_idx].get("amount"))
        except (ValueError, TypeError):
            amt = float("nan")
        if not math.isfinite(amt) or abs(abs(delta) - amt) > 0.01:
            continue
        rows[newer_idx]["amount_type"] = "credit" if delta > 0 else "debit"


# ── the main entry point (port of content.js scrapeRows) ─────────────────────
def normalize_raw_rows(rows_config: dict, raw_rows: list[dict]) -> list[dict]:
    """Turn RAW harvested cells into normalized transaction dicts.

    ``rows_config`` is the ``rows`` block of a bank's scrape config (the server
    owns it — see serve_demo._SCRAPE_CONFIGS), which drives parsing mode:
    ``amount_sign`` (credit_debit_cols | type_column | positive_is_debit),
    ``date_mode`` (parent_walk), and ``fix_year_sequence``.

    ``raw_rows`` is a list of flat dicts whose keys match the config's cell keys
    (description, credit, debit, balance, amount, amount_type, date) carrying RAW
    innerText, plus an optional ``pending`` boolean (the extension reads the
    row's CSS class — a DOM concern that must stay in the page).

    Returns ``[{date, description, amount, amount_type, raw_amount, balance,
    pending}, ...]`` — identical to what the old content.js sent.
    """
    r = rows_config or {}
    sign = r.get("amount_sign")
    parent_walk = r.get("date_mode") == "parent_walk"
    has_date_selector = bool(r.get("date"))
    results: list[dict] = []

    for raw in (raw_rows or []):
        try:
            description = ""
            date = ""
            amount = ""
            amount_type = ""
            balance = ""
            raw_amount = ""

            if r.get("description"):
                description = first_line(_cell(raw, "description"))

            # Date: parent_walk and selector both arrive as raw text from the
            # extension; the server decides how to parse it.
            dtext = _cell(raw, "date")
            if parent_walk:
                date = parse_day_month_date(dtext) or normalize_date(dtext)
            elif has_date_selector:
                date = normalize_date(dtext)

            if sign == "credit_debit_cols":
                credit_txt = _cell(raw, "credit")
                debit_txt = _cell(raw, "debit")
                balance = _cell(raw, "balance")
                if credit_txt and credit_txt != "-" and credit_txt != "0.00":
                    amount = parse_amount(credit_txt)
                    amount_type = "credit"
                    raw_amount = credit_txt
                elif debit_txt and debit_txt != "-" and debit_txt != "0.00":
                    amount = parse_amount(debit_txt)
                    amount_type = "debit"
                    raw_amount = debit_txt

            elif sign == "type_column":
                type_txt = _cell(raw, "amount_type").lower()
                raw_amount = _cell(raw, "amount")
                amount = parse_amount(raw_amount)
                desc = (description or "").lower()
                # Safer default: assume debit (most checking-account rows are
                # spends; defaulting to credit over-counts income).
                amount_type = "debit"
                if re.search(r"credit|deposit|refund|reversal", type_txt):
                    amount_type = "credit"
                elif re.search(r"debit|withdrawal|purchase", type_txt):
                    amount_type = "debit"
                elif re.search(r"transfer", type_txt):
                    # Transfer = ambiguous → look at the description.
                    if re.search(r"\bfrom\b", desc):
                        amount_type = "credit"
                    elif re.search(r"\bto\b", desc):
                        amount_type = "debit"
                # Explicit negative sign in the amount ALWAYS overrides.
                if "(" in raw_amount or raw_amount.strip().startswith("-") or "-$" in raw_amount:
                    amount_type = "debit"
                if r.get("balance"):
                    balance = _cell(raw, "balance")

            else:  # positive_is_debit (Capital One, Discover)
                raw_amount = _cell(raw, "amount")
                cleaned = re.sub(r"[$,₹€£¥()]", "", raw_amount)
                num = _js_parse_float(cleaned)
                if num is None:
                    num = 0.0
                amount = _num_to_str(abs(num))
                amount_type = "credit" if num < 0 else "debit"

            if not description and not amount:
                continue  # skip empty rows

            pending = bool(raw.get("pending")) or (date or "").lower() == "pending"

            results.append({
                "date": date,
                "description": description,
                "amount": amount,
                "amount_type": amount_type,
                "raw_amount": raw_amount,
                "balance": balance,
                "pending": pending,
            })
        except Exception:
            # One malformed row must not abort the whole batch (matches the
            # per-row try/catch in content.js scrapeRows).
            continue

    # Sequence-aware year correction for year-less date cells (ICICI).
    if r.get("fix_year_sequence"):
        fix_years_sequential(results)

    # Balance-delta direction is the truth-tier signal; runs for every bank that
    # maps a balance column (a no-op when balances aren't parseable).
    normalize_direction_from_balance(results)

    return results
