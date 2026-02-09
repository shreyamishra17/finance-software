#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional


DATE_RE = re.compile(r"\b(0?[1-9]|1[0-2])/(0?[1-9]|[12]\d|3[01])\b")
AMOUNT_RE = re.compile(r"[-(]?\$?\d{1,3}(?:,\d{3})*(?:\.\d{2})\)?")

BOILERPLATE_PATTERNS = [
    r"\bMOBILE PURCHASE\b",
    r"\bCARD PURCHASE\b",
    r"\bDEBIT CARD\b",
    r"\bCHECKCARD\b",
    r"\bPOS\b",
    r"\bPURCHASE\b",
    r"\bDES:\s*",
    r"\bID:\s*",
    r"\bINDN:\s*",
    r"\bCO ID:\s*",
    r"\bPPD\b",
    r"\bWEB\b",
    r"\bEFT\b",
    r"\bACH\b",
    r"\bVISA DIRECT\b",
    r"\bCONTINUED ON THE NEXT PAGE\b",
    r"\bWITHDRAWALS AND OTHER SUBTRACTIONS\b",
]

PHONE_RE = re.compile(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b")
MMDD_RE = re.compile(r"\b(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\b")
MM_DD_RE = re.compile(r"\b(0[1-9]|1[0-2])/(0[1-9]|[12]\d|3[01])\b")

HEADER_HINTS = (
    "statement",
    "account number",
    "account #",
    "previous balance",
    "new balance",
    "total",
    "payment",
    "balance",
    "interest",
    "credit limit",
    "minimum payment",
    "page ",
    "bank of america",
)


def _strip_token_punct(token: str) -> str:
    # Keep meaningful symbols like *, /, and . which often appear in merchant strings.
    return token.strip(" ,;:()[]{}<>")


def _is_state_abbr(token: str) -> bool:
    return token.isalpha() and token.isupper() and len(token) == 2


def _is_city_token(token: str) -> bool:
    return token.isalpha() and token.isupper() and len(token) >= 2


def _drop_location_tail(tokens: List[str]) -> List[str]:
    if len(tokens) < 2:
        return tokens
    if not _is_state_abbr(tokens[-1]):
        return tokens

    if len(tokens) >= 3 and _is_city_token(tokens[-2]) and _is_city_token(tokens[-3]):
        return tokens[:-3]
    if _is_city_token(tokens[-2]):
        return tokens[:-2]
    return tokens


def clean_description(raw: str) -> str:
    text = raw.strip()

    # Remove boilerplate phrases conservatively.
    for pattern in BOILERPLATE_PATTERNS:
        text = re.sub(pattern, " ", text, flags=re.IGNORECASE)

    # Remove phone numbers and short MM/DD or MMDD date tokens.
    text = PHONE_RE.sub(" ", text)
    text = MM_DD_RE.sub(" ", text)
    text = MMDD_RE.sub(" ", text)

    # Tokenize for numeric/ID cleanup.
    tokens: List[str] = []
    for token in text.split():
        token = _strip_token_punct(token)
        if not token:
            continue

        digits = sum(ch.isdigit() for ch in token)
        if digits >= 8:
            continue
        if len(token) >= 10 and digits / len(token) >= 0.6:
            continue
        tokens.append(token)

    # Drop trailing city/state tails like "SAN DIEGO CA" or "NEW YORK NY".
    tokens = _drop_location_tail(tokens)

    cleaned = " ".join(tokens)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -.,;:/")
    return cleaned


def clean_transaction_row(txn: dict) -> dict:
    cleaned = dict(txn)
    raw = str(txn.get("raw_description", ""))
    cleaned["description_clean"] = clean_description(raw)
    return cleaned


@dataclass
class StatementInfo:
    bank_name: Optional[str] = None
    account_name: Optional[str] = None
    owner_name: Optional[str] = None
    statement_period: Optional[str] = None
    account_number: Optional[str] = None


@dataclass
class AccountSummary:
    beginning_balance: Optional[str] = None
    beginning_balance_date: Optional[str] = None
    deposits_additions: Optional[str] = None
    withdrawals_subtractions: Optional[str] = None
    checks: Optional[str] = None
    service_fees: Optional[str] = None
    ending_balance: Optional[str] = None
    ending_balance_date: Optional[str] = None


@dataclass
class Transaction:
    date: str
    description: str
    amount: str


def extract_text(pdf_path: Path) -> List[str]:
    try:
        import pdfplumber  # type: ignore
    except Exception as exc:  # pragma: no cover - dependency path
        raise RuntimeError(
            "Missing dependency: pdfplumber. Install with: python3 -m pip install pdfplumber"
        ) from exc

    lines: List[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            for line in text.splitlines():
                cleaned = re.sub(r"\s+", " ", line).strip()
                if cleaned:
                    lines.append(cleaned)
    return lines


def parse_statement_info(lines: Iterable[str]) -> StatementInfo:
    info = StatementInfo()
    period_re = re.compile(r"([A-Z][a-z]+ \d{1,2}, \d{4} to [A-Z][a-z]+ \d{1,2}, \d{4})")
    acct_re = re.compile(r"(?:account number:|account #)\s*([0-9 ]{6,})", re.IGNORECASE)
    owner_re = re.compile(r"^[A-Z][A-Z .'-]+$")

    for line in lines:
        lower = line.lower()
        if info.bank_name is None and "bank of america" in lower:
            info.bank_name = "Bank of America"

        if info.statement_period is None:
            if "statement period" in lower:
                info.statement_period = line.split(":", 1)[-1].strip()
            else:
                match = period_re.search(line)
                if match:
                    info.statement_period = match.group(1)

        if info.account_name is None and "account name" in lower:
            info.account_name = line.split(":", 1)[-1].strip()

        if info.account_number is None:
            match = acct_re.search(line)
            if match:
                info.account_number = re.sub(r"\s+", " ", match.group(1)).strip()

        if info.owner_name is None:
            if "account holder" in lower or "primary account" in lower:
                info.owner_name = line.split(":", 1)[-1].strip()
            elif "bankofamerica" not in lower and owner_re.match(line) and not re.search(r"\d", line):
                info.owner_name = line.strip()

    return info


def is_noise(line: str) -> bool:
    lower = line.lower()
    return any(hint in lower for hint in HEADER_HINTS)


def parse_transactions(lines: Iterable[str]) -> List[Transaction]:
    transactions: List[Transaction] = []
    pending: Optional[Transaction] = None

    for line in lines:
        if is_noise(line):
            continue

        date_match = DATE_RE.search(line)
        amount_matches = list(AMOUNT_RE.finditer(line))

        if date_match and amount_matches:
            amount = amount_matches[-1].group(0)
            # Remove date and amount tokens to leave description.
            description = line
            description = DATE_RE.sub("", description, count=1)
            description = description.replace(amount, "")
            description = re.sub(r"\s+", " ", description).strip(" -")

            txn = Transaction(
                date=date_match.group(0),
                description=description.strip(),
                amount=amount.strip(),
            )
            transactions.append(txn)
            pending = txn
            continue

        # Continuation lines: attach to previous description when they look like details.
        if pending and not DATE_RE.search(line) and not AMOUNT_RE.search(line):
            if not is_noise(line):
                pending.description = f"{pending.description} {line}".strip()

    return transactions


def parse_account_summary(lines: Iterable[str]) -> AccountSummary:
    summary = AccountSummary()
    in_summary = False

    for line in lines:
        lower = line.lower()

        if lower.startswith("account summary"):
            in_summary = True
            continue

        if in_summary and ("important information" in lower or "date description amount" in lower):
            break

        if not in_summary:
            continue

        if "beginning balance on" in lower:
            summary.beginning_balance = line.split()[-1]
            match = re.search(r"beginning balance on (.+?)\s+\$?[-0-9,]+\.\d{2}", line, re.IGNORECASE)
            if match:
                summary.beginning_balance_date = match.group(1).strip()
        elif "deposits and other additions" in lower:
            summary.deposits_additions = line.split()[-1]
        elif "withdrawals and other subtractions" in lower:
            summary.withdrawals_subtractions = line.split()[-1]
        elif lower.startswith("checks"):
            summary.checks = line.split()[-1]
        elif lower.startswith("service fees"):
            summary.service_fees = line.split()[-1]
        elif "ending balance on" in lower:
            summary.ending_balance = line.split()[-1]
            match = re.search(r"ending balance on (.+?)\s+\$?[-0-9,]+\.\d{2}", line, re.IGNORECASE)
            if match:
                summary.ending_balance_date = match.group(1).strip()

    return summary


def print_statement(info: StatementInfo, summary: AccountSummary, transactions: List[Transaction]) -> None:
    print("Statement Summary")
    print("-----------------")
    print(f"Bank Name: {info.bank_name or 'Unknown'}")
    if info.account_number:
        print(f"Account Number: {info.account_number}")
    if info.account_name:
        print(f"Account Name: {info.account_name}")
    if info.owner_name:
        print(f"Owner Name: {info.owner_name}")
    if info.statement_period:
        print(f"Statement Period: {info.statement_period}")
    if any(
        [
            summary.beginning_balance,
            summary.deposits_additions,
            summary.withdrawals_subtractions,
            summary.checks,
            summary.service_fees,
            summary.ending_balance,
        ]
    ):
        print("")
        print("Account Summary")
        print("---------------")
        if summary.beginning_balance:
            if summary.beginning_balance_date:
                print(f"Beginning Balance ({summary.beginning_balance_date}): {summary.beginning_balance}")
            else:
                print(f"Beginning Balance: {summary.beginning_balance}")
        if summary.deposits_additions:
            print(f"Deposits and Other Additions: {summary.deposits_additions}")
        if summary.withdrawals_subtractions:
            print(f"Withdrawals and Other Subtractions: {summary.withdrawals_subtractions}")
        if summary.checks:
            print(f"Checks: {summary.checks}")
        if summary.service_fees:
            print(f"Service Fees: {summary.service_fees}")
        if summary.ending_balance:
            if summary.ending_balance_date:
                print(f"Ending Balance ({summary.ending_balance_date}): {summary.ending_balance}")
            else:
                print(f"Ending Balance: {summary.ending_balance}")
    print("")
    print("Transactions")
    print("------------")
    for txn in transactions:
        print(f"{txn.date} | {txn.description} | {txn.amount}")


DEFAULT_PDF_PATH = Path("my_finance/Apr10-25.pdf")


def run_clean_description_demo() -> None:
    samples = [
        "MOBILE PURCHASE 0311 TST* THE YASAI - CONVOY SAN DIEGO CA",
        "CHECKCARD 0331 INTUIT *TURBOTAX CL.INTUIT.COMCA 24692165090103912217905",
        "PMNT SENT 0319 VENMO *Jenny P Visa Direct NY continued on the next page Withdrawals and other subtractions",
        "MOBILE PURCHASE 0330 SQ *BLUE BOTTLE COFFEE San Diego CA",
        "PURCHASE 0331 APPLE.COM/BILL 866-712-7753 CA",
        "WHOLEFDS LAJ 1 03/18 #000017919 MOBILE PURCHASE WHOLEFDS LAJ 100 LA JOLLA CA",
    ]

    print("Clean Description Demo")
    print("----------------------")
    for sample in samples:
        print(f"Before: {sample}")
        print(f"After:  {clean_description(sample)}")
        print("")


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--demo-clean":
        run_clean_description_demo()
        return 0

    if len(sys.argv) < 2:
        pdf_path = DEFAULT_PDF_PATH
    else:
        pdf_path = Path(sys.argv[1]).expanduser()
    if not pdf_path.exists():
        print(f"File not found: {pdf_path}")
        print("Pass a path explicitly: python3 parse_boa_pdf.py <statement.pdf>")
        return 1

    lines = extract_text(pdf_path)
    info = parse_statement_info(lines)
    summary = parse_account_summary(lines)
    transactions = parse_transactions(lines)
    print_statement(info, summary, transactions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
