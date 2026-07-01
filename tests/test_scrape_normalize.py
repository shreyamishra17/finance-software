"""Parity tests for src/scrape_normalize.py — the server-side port of the
content.js parsing logic used by the thin-extension raw-ingest path.

These lock the normalized output to exactly what the legacy content.js produced
(same txn dict shape, same credit/debit/amount/date/currency handling) so the
old (pre-parsed) and new (raw) ingest paths agree during the migration.

Runs standalone (`python3 tests/test_scrape_normalize.py`) and under pytest.
"""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from scrape_normalize import (  # noqa: E402
    parse_amount,
    normalize_date,
    parse_day_month_date,
    fix_years_sequential,
    normalize_direction_from_balance,
    normalize_raw_rows,
    first_line,
)

# Real per-bank rows configs (mirror src/serve_demo.py _scrape_configs()).
ICICI_ROWS = {
    "selector": "tr.transfer:not(.trans_space)",
    "description": "td.remarks-details p.name",
    "credit": "td.credit p",
    "debit": "td.debit p",
    "balance": "td.balance p",
    "date_mode": "parent_walk",
    "fix_year_sequence": True,
    "amount_sign": "credit_debit_cols",
}
BOA_ROWS = {
    "selector": "tr.activity-row",
    "date": "td:nth-child(1)",
    "description": "td:nth-child(2)",
    "amount_type": "td:nth-child(3)",
    "amount": "td:nth-child(4)",
    "balance": "td:nth-child(5)",
    "amount_sign": "type_column",
}
CAP1_ROWS = {
    "selector": "c1-ease-row",
    "date": ".cdk-column-date",
    "description": ".cdk-column-description",
    "amount": ".cdk-column-amount",
    "amount_sign": "positive_is_debit",
}


def _check(name, cond):
    if not cond:
        raise AssertionError(f"FAILED: {name}")
    print(f"  ok: {name}")


# ── low-level ports ──────────────────────────────────────────────────────────
def test_parse_amount():
    _check("parse ₹ amount", parse_amount("₹2,000.00") == "2000")
    _check("parse $ amount", parse_amount("$1,234.56") == "1234.56")
    _check("parse parens (abs, not signed)", parse_amount("($50.00)") == "50")
    _check("parse negative → abs", parse_amount("-$30.00") == "30")
    _check("parse blank → 0", parse_amount("") == "0")
    _check("parse junk → 0", parse_amount("-") == "0")


def test_first_line():
    _check("firstLine multi-line", first_line("UPI/foo/ref\n304027881\nmore") == "UPI/foo/ref")


def test_normalize_date():
    _check("ISO passthrough", normalize_date("2025-12-25") == "2025-12-25")
    _check("US slash date", normalize_date("06/15/2026") == "2026-06-15")
    _check("pending sentinel", normalize_date("Pending") == "Pending")
    _check("processing → pending", normalize_date("Processing 06/01") == "Pending")
    _check("empty → empty", normalize_date("") == "")


def test_parse_day_month_date():
    now = datetime.now()

    def expected(mon0, day):
        y = now.year - 1 if mon0 > (now.month - 1) else now.year
        return f"{y}-{mon0 + 1:02d}-{day:02d}"

    _check("29 Jun", parse_day_month_date("29 Jun") == expected(5, 29))
    _check("Dec 29 (reversed)", parse_day_month_date("Dec 29") == expected(11, 29))
    _check("noisy '14 Jun 14 SUN'", parse_day_month_date("14 Jun 14 SUN") == expected(5, 14))
    _check("no month/day → ''", parse_day_month_date("SUN") == "")


def test_fix_years_sequential():
    rows = [
        {"date": "2026-06-29"},
        {"date": "2026-05-20"},
        {"date": "2026-12-15"},   # month jumps UP going older → prior year
        {"date": "2026-11-10"},
        {"date": "Pending"},      # skipped, untouched
    ]
    fix_years_sequential(rows)
    _check("anchor year kept", rows[0]["date"] == "2026-06-29")
    _check("same-year non-increase", rows[1]["date"] == "2026-05-20")
    _check("year decremented on wrap", rows[2]["date"] == "2025-12-15")
    _check("stays in prior year", rows[3]["date"] == "2025-11-10")
    _check("pending untouched", rows[4]["date"] == "Pending")


def test_direction_from_balance():
    rows = [
        {"date": "2026-06-20", "amount": "500",  "amount_type": "credit", "balance": "₹9,500.00"},
        {"date": "2026-06-18", "amount": "2000", "amount_type": "debit",  "balance": "₹10,000.00"},
        {"date": "2026-06-15", "amount": "1000", "amount_type": "credit", "balance": "₹8,000.00"},
    ]
    normalize_direction_from_balance(rows)
    # balance fell 10000→9500 → debit; rose 8000→10000 → credit
    _check("balance drop → debit", rows[0]["amount_type"] == "debit")
    _check("balance rise → credit", rows[1]["amount_type"] == "credit")


# ── full normalize_raw_rows per bank ─────────────────────────────────────────
def test_icici_credit_debit_cols():
    raw = [
        {"description": "UPI/payee/ref\n304027881", "credit": "", "debit": "₹500.00",
         "balance": "₹9,500.00", "date": "20 Jun", "pending": False},
        {"description": "NEFT salary", "credit": "₹2,000.00", "debit": "",
         "balance": "₹10,000.00", "date": "18 Jun", "pending": False},
        {"description": "ATM WDL", "credit": "", "debit": "₹1,000.00",
         "balance": "₹8,000.00", "date": "15 Jun", "pending": False},
        {"description": "", "credit": "", "debit": "", "balance": "", "date": "10 Jun"},  # empty → skip
    ]
    out = normalize_raw_rows(ICICI_ROWS, raw)
    _check("icici: empty row skipped", len(out) == 3)
    _check("icici: firstLine desc", out[0]["description"] == "UPI/payee/ref")
    _check("icici: debit amount parsed", out[0]["amount"] == "500" and out[0]["amount_type"] == "debit")
    _check("icici: credit amount parsed", out[1]["amount"] == "2000" and out[1]["amount_type"] == "credit")
    _check("icici: raw_amount preserved (₹ for currency)", out[1]["raw_amount"] == "₹2,000.00")
    _check("icici: balance preserved", out[0]["balance"] == "₹9,500.00")
    _check("icici: date parsed from parent-walk text", out[0]["date"].endswith("-06-20"))


def test_icici_year_wrap_integration():
    # newest-first across a Jun→Dec wrap; parent_walk + fix_year_sequence.
    raw = [
        {"description": "a", "debit": "₹10.00", "credit": "", "balance": "", "date": "05 Feb"},
        {"description": "b", "debit": "₹10.00", "credit": "", "balance": "", "date": "20 Dec"},
    ]
    out = normalize_raw_rows(ICICI_ROWS, raw)
    feb_year = int(out[0]["date"][:4])
    dec_year = int(out[1]["date"][:4])
    _check("icici: Dec is a year before the Feb above it", dec_year == feb_year - 1)


def test_boa_type_column():
    raw = [
        {"description": "PAYROLL DEP", "amount_type": "Credit", "amount": "$3,000.00",
         "balance": "", "date": "06/15/2026"},
        {"description": "STARBUCKS", "amount_type": "Debit", "amount": "$5.00",
         "balance": "", "date": "06/14/2026"},
        {"description": "TRANSFER FROM SAVINGS", "amount_type": "Transfer", "amount": "$100.00",
         "balance": "", "date": "06/13/2026"},
        {"description": "TRANSFER TO CARD", "amount_type": "Transfer", "amount": "$50.00",
         "balance": "", "date": "06/12/2026"},
        {"description": "ODD", "amount_type": "Credit", "amount": "($20.00)",
         "balance": "", "date": "06/11/2026"},  # paren overrides credit → debit
    ]
    out = normalize_raw_rows(BOA_ROWS, raw)
    _check("boa: credit type", out[0]["amount_type"] == "credit" and out[0]["amount"] == "3000")
    _check("boa: debit type", out[1]["amount_type"] == "debit")
    _check("boa: transfer FROM → credit", out[2]["amount_type"] == "credit")
    _check("boa: transfer TO → debit", out[3]["amount_type"] == "debit")
    _check("boa: paren overrides to debit", out[4]["amount_type"] == "debit")
    _check("boa: date normalized", out[0]["date"] == "2026-06-15")


def test_capital_one_positive_is_debit():
    raw = [
        {"description": "Coffee", "amount": "$50.00", "date": "2026-06-10"},
        {"description": "Refund", "amount": "-$30.00", "date": "2026-06-09"},
        {"description": "Parens", "amount": "($10.00)", "date": "2026-06-08"},
    ]
    out = normalize_raw_rows(CAP1_ROWS, raw)
    _check("cap1: positive → debit", out[0]["amount_type"] == "debit" and out[0]["amount"] == "50")
    _check("cap1: negative → credit", out[1]["amount_type"] == "credit" and out[1]["amount"] == "30")
    _check("cap1: parens not negative → debit", out[2]["amount_type"] == "debit" and out[2]["amount"] == "10")


def test_against_real_server_configs():
    """Drift guard: run normalize_raw_rows against the ACTUAL configs the server
    serves (serve_demo._scrape_configs), not the copies above. Skips if the
    server module's heavy deps aren't importable in this environment."""
    try:
        import inspect
        import serve_demo as sd
    except Exception as e:
        print(f"  skip (serve_demo not importable: {type(e).__name__})")
        return
    cls = [o for _, o in vars(sd).items()
           if inspect.isclass(o) and hasattr(o, "_scrape_configs")][0]
    cfgs = cls._scrape_configs(object.__new__(cls))
    by_key = {c.get("bank_key"): c for c in cfgs.values()}

    icici = normalize_raw_rows(by_key["icici"]["rows"], [
        {"description": "NEFT salary", "credit": "₹2,000.00", "debit": "",
         "balance": "₹10,000.00", "date": "18 Jun"},
    ])
    _check("real icici config → credit parsed",
           icici and icici[0]["amount"] == "2000" and icici[0]["amount_type"] == "credit")

    boa = normalize_raw_rows(by_key["boa"]["rows"], [
        {"description": "PAYROLL", "amount_type": "Credit", "amount": "$3,000.00",
         "balance": "", "date": "06/15/2026"},
    ])
    _check("real boa config → credit parsed", boa and boa[0]["amount_type"] == "credit")

    cap1 = normalize_raw_rows(by_key["capital_one"]["rows"], [
        {"description": "Coffee", "amount": "$50.00", "date": "2026-06-10"},
    ])
    _check("real cap1 config → debit parsed", cap1 and cap1[0]["amount_type"] == "debit")


def _all_tests():
    return [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


if __name__ == "__main__":
    failures = 0
    for fn in _all_tests():
        print(f"\n{fn.__name__}:")
        try:
            fn()
        except AssertionError as e:
            print(f"  {e}")
            failures += 1
    print(f"\n{'ALL PASSED' if not failures else f'{failures} TEST GROUP(S) FAILED'}")
    sys.exit(1 if failures else 0)
