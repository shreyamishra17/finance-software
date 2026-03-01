
from collections import Counter, defaultdict
from pathlib import Path

from categorize import categorize_transactions
from parse_bank import parse_pdf_to_payload


payload = parse_pdf_to_payload(Path("my_finance/raw/Apr10-25.pdf"))
payload["transactions"] = categorize_transactions(payload["transactions"])

counts = Counter(t.get("category", "Uncategorized") for t in payload["transactions"])
print(counts)

by_category: dict[str, list[dict]] = defaultdict(list)
for txn in payload["transactions"]:
    category = txn.get("category", "Uncategorized")
    by_category[category].append(txn)

for category in sorted(by_category.keys()):
    print("")
    print(f"{category} ({len(by_category[category])})")
    print("-" * (len(category) + 4))
    for txn in by_category[category]:
        date = txn.get("date", "")
        merchant = txn.get("merchant_clean") or txn.get("raw_description", "")
        amount = txn.get("amount", "")
        print(f"{date} | {merchant} | {amount}")
