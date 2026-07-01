#!/usr/bin/env python3
"""Read-only sanity check for a synced account (default: ICICI).

Run it in the SAME shell/env where your local FIFO server runs, so it inherits
the Firestore credentials/project:

    python3 scripts/verify_sync.py            # checks the ICICI account
    python3 scripts/verify_sync.py boa        # or any bank_key

It NEVER writes. It reconciles the stored transactions against the bank's own
running-balance column — the truth tier — so you can confirm the thin-extension
raw path produced correct data before you republish.
"""
import sys, os, re
_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(_ROOT, "src"))
# Load the same .env the server does BEFORE importing db, so we hit the same
# Firestore project/database (GOOGLE_CLOUD_PROJECT + FIRESTORE_DATABASE) instead
# of the ambient gcloud ADC default.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, ".env"))
except Exception:
    pass
import db

WANT = (sys.argv[1] if len(sys.argv) > 1 else "icici").lower()


def num(s):
    s = re.sub(r"[^\d.\-]", "", str(s or "").replace("(", "-"))
    try:
        return float(s)
    except ValueError:
        return None


def find_account():
    for uid in db.list_user_ids():
        db.set_current_uid(uid)
        for a in db.get_accounts():
            if WANT in (a.get("bank_key") or "").lower():
                return uid, a
    return None, None


uid, acct = find_account()
if not acct:
    print(f"No account matching bank_key '{WANT}' found.")
    sys.exit(1)

db.set_current_uid(uid)
txns = db.get_transactions_for_account_raw(acct["id"])

# Newest-first by date (Pending sorts to top).
def datekey(t):
    d = (t.get("date") or "")
    return d if re.match(r"\d{4}-\d{2}-\d{2}", d) else "9999-99-99"

txns.sort(key=datekey, reverse=True)

print(f"\n=== {acct.get('bank_key')} account #{acct['id']} ===")
print(f"stored account_balance : {acct.get('account_balance')!r}   currency: {acct.get('currency')!r}")
print(f"transactions           : {len(txns)}")

# 1) Currency + year distribution (catches ₹→$ and year-off bugs)
years, syms = {}, {}
for t in txns:
    m = re.match(r"(\d{4})", t.get("date") or "")
    if m:
        years[m.group(1)] = years.get(m.group(1), 0) + 1
    for s in ("₹", "$", "€", "£", "¥"):
        if s in str(t.get("balance") or "") or s in str(t.get("raw_amount") or ""):
            syms[s] = syms.get(s, 0) + 1
print(f"years                  : {dict(sorted(years.items()))}")
print(f"currency symbols seen  : {syms or '(none in stored raw fields)'}")

# 2) Credit/debit split + money in/out
cin = sum(num(t.get("amount")) or 0 for t in txns if (t.get("amount_type") or "").lower() == "credit")
cout = sum(num(t.get("amount")) or 0 for t in txns if (t.get("amount_type") or "").lower() == "debit")
ncred = sum(1 for t in txns if (t.get("amount_type") or "").lower() == "credit")
print(f"credits / debits       : {ncred} / {len(txns) - ncred}")
print(f"money in (Σcredit)     : {cin:,.2f}")
print(f"money out (Σdebit)     : {cout:,.2f}")
end = num(acct.get("account_balance"))
if end is not None:
    print(f"implied starting bal   : {end - (cin - cout):,.2f}   <-- should be a plausible (usually positive) opening balance")

# 3) Ending balance should equal the newest row's running balance
newest_bal = next((t.get("balance") for t in txns if str(t.get("balance") or "").strip()), None)
print(f"newest row balance     : {newest_bal!r}   (should equal stored account_balance AND your real ICICI balance)")

# 4) Balance-delta reconciliation: newer.balance - older.balance must equal the
#    signed amount. This proves each row's direction against the bank's column.
ok = bad = untrusted = 0
mism = []
for i in range(len(txns) - 1):
    nb, ob = num(txns[i].get("balance")), num(txns[i + 1].get("balance"))
    amt = num(txns[i].get("amount"))
    if nb is None or ob is None or amt is None:
        untrusted += 1
        continue
    delta = nb - ob
    if abs(abs(delta) - amt) > 0.01:
        untrusted += 1  # same-day reorder or gap — can't judge this pair
        continue
    truth = "credit" if delta > 0 else "debit"
    if (txns[i].get("amount_type") or "").lower() == truth:
        ok += 1
    else:
        bad += 1
        if len(mism) < 12:
            mism.append((txns[i].get("date"), (txns[i].get("description") or "")[:40],
                         txns[i].get("amount_type"), f"delta={delta:+.2f}", f"amt={amt}"))
print(f"\nbalance reconcile      : {ok} correct, {bad} WRONG direction, {untrusted} unverifiable (same-day/gaps)")
if mism:
    print("  rows whose stored direction DISAGREES with the balance column:")
    for r in mism:
        print("   ", r)

# 5) Category vs direction anomaly (e.g. 'Income' on a money-out row)
anom = [(t.get("date"), (t.get("description") or "")[:40], t.get("category"), t.get("amount_type"), t.get("amount"))
        for t in txns
        if (t.get("category") or "").lower() == "income" and (t.get("amount_type") or "").lower() == "debit"]
print(f"\nIncome-but-debit rows  : {len(anom)}")
for r in anom[:12]:
    print("   ", r)

print("\nDone (read-only — nothing was modified).")
