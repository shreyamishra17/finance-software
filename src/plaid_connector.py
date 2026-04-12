"""
Plaid API connector for FIFO.

Handles all Plaid API calls:
  - create_link_token  → initialises Plaid Link popup in the browser
  - exchange_public_token → swaps the one-time public_token for a permanent access_token
  - save/load access_token → stored in macOS Keychain via keyring (never on disk)
  - fetch_transactions → returns transactions in FIFO's internal format
  - fetch_balance      → returns current account balance as a formatted string
"""
from __future__ import annotations

import os
from datetime import datetime

import keyring
import plaid
from plaid.api import plaid_api
from plaid.api_client import ApiClient
from plaid.configuration import Configuration
from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.products import Products
from plaid.model.country_code import CountryCode
from plaid.model.transactions_get_request import TransactionsGetRequest
from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions
from plaid.model.sandbox_public_token_create_request import SandboxPublicTokenCreateRequest
from plaid.model.sandbox_item_fire_webhook_request import SandboxItemFireWebhookRequest
from plaid.model.transactions_refresh_request import TransactionsRefreshRequest

KEYRING_SERVICE = "fifo_plaid"
_PAGE_SIZE = 500


def _get_client() -> plaid_api.PlaidApi:
    env_name = os.environ.get("PLAID_ENV", "sandbox")
    host = {
        "sandbox":     plaid.Environment.Sandbox,
        "development": "https://development.plaid.com",
        "production":  plaid.Environment.Production,
    }[env_name]
    cfg = Configuration(host=host)
    cfg.api_key["clientId"] = os.environ["PLAID_CLIENT_ID"]
    cfg.api_key["secret"] = os.environ["PLAID_SECRET"]
    cfg.api_key["Plaid-Version"] = "2020-09-14"  # keep for compatibility; PFC handled via dict access
    return plaid_api.PlaidApi(ApiClient(cfg))


def create_link_token(user_id: str = "fifo-user") -> str:
    """Create a one-time Plaid Link token for the frontend popup."""
    client = _get_client()
    req = LinkTokenCreateRequest(
        user=LinkTokenCreateRequestUser(client_user_id=user_id),
        client_name="FIFO",
        products=[Products("transactions")],
        country_codes=[CountryCode("US")],
        language="en",
    )
    resp = client.link_token_create(req)
    return resp["link_token"]


def exchange_public_token(public_token: str) -> tuple[str, str]:
    """Exchange a public_token from Plaid Link for a permanent (access_token, item_id)."""
    client = _get_client()
    resp = client.item_public_token_exchange(
        ItemPublicTokenExchangeRequest(public_token=public_token)
    )
    return resp["access_token"], resp["item_id"]


def save_access_token(account_id: int, access_token: str) -> None:
    """Store the Plaid access token in macOS Keychain (never written to disk)."""
    keyring.set_password(KEYRING_SERVICE, f"access_token_{account_id}", access_token)


def load_access_token(account_id: int) -> str | None:
    """Load the Plaid access token from macOS Keychain."""
    return keyring.get_password(KEYRING_SERVICE, f"access_token_{account_id}")


def fetch_transactions(account_id: int, since_date: datetime) -> list[dict]:
    """
    Fetch all transactions since since_date from Plaid, paginating as needed.
    Returns a list of dicts in FIFO's internal transaction format.
    """
    import time
    access_token = load_access_token(account_id)
    if not access_token:
        raise ValueError(f"No Plaid access token found for account {account_id}")

    client = _get_client()
    start = since_date.date()
    end = datetime.now().date()

    # First page — retry up to 5x if transactions not ready yet (sandbox delay)
    req = TransactionsGetRequest(
        access_token=access_token,
        start_date=start,
        end_date=end,
        options=TransactionsGetRequestOptions(count=_PAGE_SIZE, offset=0),
    )
    resp = None
    for attempt in range(5):
        try:
            resp = client.transactions_get(req)
            break
        except plaid.ApiException as e:
            if "PRODUCT_NOT_READY" in str(e):
                print(f"Plaid: transactions not ready yet, retrying in 5s (attempt {attempt + 1}/5)…")
                time.sleep(5)
            else:
                raise
    if resp is None:
        raise RuntimeError("Plaid transactions not ready after retries — try again in a moment")
    txns = list(resp["transactions"])
    total = resp["total_transactions"]

    # Remaining pages
    while len(txns) < total:
        req2 = TransactionsGetRequest(
            access_token=access_token,
            start_date=start,
            end_date=end,
            options=TransactionsGetRequestOptions(count=_PAGE_SIZE, offset=len(txns)),
        )
        txns.extend(client.transactions_get(req2)["transactions"])

    return [_map_transaction(t) for t in txns]


# Maps Plaid personal_finance_category.primary → (forced_amount_type | None, FIFO category name)
# None means "follow sign convention"; "credit"/"debit" means override the sign.
_PFC_MAP: dict[str, tuple[str | None, str]] = {
    "INCOME":                        ("credit", "Income"),
    "TRANSFER_IN":                   ("credit", "Transfer"),
    "TRANSFER_OUT":                  ("debit",  "Transfer"),
    "LOAN_PAYMENTS":                 ("debit",  "Debt Payments"),
    "BANK_FEES":                     ("debit",  "Bank Fees"),
    "ENTERTAINMENT":                 ("debit",  "Entertainment"),
    "FOOD_AND_DRINK":                ("debit",  "Food & Drink"),
    "GROCERY":                       ("debit",  "Groceries"),
    "GENERAL_MERCHANDISE":           ("debit",  "Shopping"),
    "ONLINE_MARKETPLACES":           ("debit",  "Shopping"),
    "HOME_IMPROVEMENT":              ("debit",  "Home"),
    "MEDICAL":                       ("debit",  "Health"),
    "PERSONAL_CARE":                 ("debit",  "Personal Care"),
    "GENERAL_SERVICES":              ("debit",  "Services"),
    "GOVERNMENT_AND_NON_PROFIT":     ("debit",  "Charity & Donations"),
    "TRANSPORTATION":                ("debit",  "Transport"),
    "TRAVEL":                        ("debit",  "Travel"),
    "RENT_AND_UTILITIES":            ("debit",  "Bills"),
    "SUBSCRIPTION":                  ("debit",  "Subscriptions"),
    "EDUCATION":                     ("debit",  "Education"),
    "SPORTING_GOODS":                ("debit",  "Shopping"),
    "PET_SUPPLIES":                  ("debit",  "Shopping"),
    "CASH_AND_ATM":                  ("debit",  "Cash"),
    "TAX":                           ("debit",  "Government"),
}

# Maps Plaid legacy category strings → FIFO category names
_LEGACY_CAT_MAP: dict[str, str] = {
    # Food
    "Restaurants": "Food & Drink",
    "Fast Food": "Food & Drink",
    "Coffee Shop": "Food & Drink",
    "Food and Drink": "Food & Drink",
    "Bar": "Food & Drink",
    "Breweries": "Food & Drink",
    "Winery": "Food & Drink",
    # Groceries
    "Supermarkets and Groceries": "Groceries",
    "Discount Stores": "Groceries",
    # Travel
    "Airlines and Aviation Services": "Travel",
    "Travel": "Travel",
    "Hotels": "Travel",
    "Car Rental": "Travel",
    "Lodging": "Travel",
    "Vacation Rentals": "Travel",
    # Transport
    "Taxis and Ride Shares": "Transport",
    "Gas Stations": "Transport",
    "Public Transportation Services": "Transport",
    "Parking": "Transport",
    "Tolls and Fees": "Transport",
    "Auto": "Transport",
    # Healthcare
    "Gyms and Fitness Centers": "Health",
    "Pharmacies": "Health",
    "Hospitals": "Health",
    "Healthcare Services": "Health",
    "Dentists": "Health",
    "Optometrists": "Health",
    "Mental Health": "Health",
    "Healthcare": "Health",
    # Shopping
    "Shops": "Shopping",
    "Clothing and Accessories": "Shopping",
    "Electronics": "Shopping",
    "Digital Purchase": "Shopping",
    "Department Stores": "Shopping",
    "Sporting Goods": "Shopping",
    "Furniture": "Shopping",
    "Home Supplies": "Shopping",
    "Book Stores": "Shopping",
    "Pet Supplies": "Shopping",
    # Subscriptions
    "Subscription": "Subscriptions",
    "Cable": "Subscriptions",
    "Streaming": "Subscriptions",
    "Software": "Subscriptions",
    "Internet Services": "Subscriptions",
    "Music": "Subscriptions",
    "Newspapers": "Subscriptions",
    # Entertainment
    "Entertainment": "Entertainment",
    "Arts and Entertainment": "Entertainment",
    "Amusement": "Entertainment",
    "Games": "Entertainment",
    "Movies and DVDs": "Entertainment",
    "Music and Audio": "Entertainment",
    "Sporting Events, Amusement Parks and Museums": "Entertainment",
    # Bills / Utilities
    "Utilities": "Bills",
    "Electric": "Bills",
    "Gas": "Bills",
    "Water": "Bills",
    "Telecommunication Services": "Bills",
    "Insurance": "Bills",
    "Rent": "Bills",
    "Rent and Utilities": "Bills",
    # Transfers / Debt
    "Transfer": "Transfer",
    "Credit": "Transfer",
    "Debit": "Transfer",
    "Wire": "Transfer",
    "Credit Card": "Debt Payments",
    "Loan": "Debt Payments",
    "Mortgage": "Debt Payments",
    # Services
    "Service": "Services",
    "Personal Care": "Personal Care",
    "Hair": "Personal Care",
    "Spa and Massage": "Personal Care",
    "Laundry and Dry Cleaning": "Personal Care",
    # Income
    "Deposit": "Income",
    "Payroll": "Income",
    "Check": "Income",
    "Interest": "Income",
    "Dividend": "Income",
    # Charity
    "Charity": "Charity & Donations",
    "Religious": "Charity & Donations",
    # Education
    "Education": "Education",
    "Colleges and Universities": "Education",
    "Tuition": "Education",
    # Other
    "Government Departments and Agencies": "Government",
    "Taxes": "Government",
    "ATM": "Cash",
    "Cash Advance": "Cash",
}


def _map_transaction(t) -> dict:
    """
    Map a Plaid transaction object to FIFO's internal dict format.

    Plaid sign convention: positive amount = money OUT (debit/expense),
    negative amount = money IN (credit/income). FIFO stores amounts as
    absolute values with an amount_type field.

    personal_finance_category (API 2022-05-18+) overrides sign when present —
    most importantly for INCOME transactions which Plaid may report as positive.
    """
    amount = float(t["amount"])
    sign_type = "debit" if amount > 0 else "credit"

    # ── personal_finance_category (preferred, API 2022-05-18+) ───────────
    pfc = None
    try:
        pfc = t.get("personal_finance_category") or t.get("personal_finance_category_icon_url") and None
        if pfc and not isinstance(pfc, dict):
            pfc = dict(pfc)
    except Exception:
        pfc = None

    pfc_primary = (pfc or {}).get("primary") if pfc else None
    forced_type, category = _PFC_MAP.get(pfc_primary or "", (None, None))

    if forced_type:
        amount_type = forced_type
    else:
        amount_type = sign_type

    # ── Legacy category array fallback ───────────────────────────────────
    if category is None:
        raw_cats: list[str] = []
        try:
            raw_cats = list(t.get("category") or [])
        except Exception:
            pass
        # Try each entry from most specific to least specific
        for c in reversed(raw_cats):
            mapped = _LEGACY_CAT_MAP.get(c)
            if mapped:
                category = mapped
                break
        if category is None:
            # Final fallback: use raw most-specific entry if present
            category = raw_cats[-1] if raw_cats else "Uncategorized"

    merchant = t.get("merchant_name") or t.get("name") or ""

    # ── Merchant-name keyword fallback for remaining Uncategorized ────
    if not category or category == "Uncategorized":
        mn = merchant.lower()
        if any(k in mn for k in ("airline", "airways", "united", "delta", "southwest", "american air", "jetblue", "spirit air", "frontier")):
            category = "Travel"
        elif any(k in mn for k in ("hotel", "marriott", "hilton", "hyatt", "sheraton", "westin", "airbnb", "vrbo")):
            category = "Travel"
        elif any(k in mn for k in ("bicycle", "bike shop", "cycle", "trek ", "specialized")):
            category = "Shopping"
        elif any(k in mn for k in ("netflix", "spotify", "hulu", "disney+", "apple.com/bill", "amazon prime", "youtube premium", "hbo", "peacock", "paramount")):
            category = "Subscriptions"
        elif any(k in mn for k in ("amazon", "walmart", "target", "costco", "best buy", "home depot", "lowe's", "ikea", "wayfair")):
            category = "Shopping"
        elif any(k in mn for k in ("whole foods", "trader joe", "safeway", "kroger", "publix", "aldi", "wegmans", "sprouts")):
            category = "Groceries"
        elif any(k in mn for k in ("uber", "lyft", "taxi", "mta", "caltrain", "bart", "metro", "transit")):
            category = "Transport"
        elif any(k in mn for k in ("gusto", "payroll", "direct deposit", "ach credit", "tectra")):
            category = "Income"
            amount_type = "credit"
        elif any(k in mn for k in ("cvs", "walgreen", "rite aid", "pharmacy", "hospital", "clinic", "dental", "urgent care")):
            category = "Health"
        elif any(k in mn for k in ("pg&e", "con ed", "at&t", "verizon", "comcast", "xfinity", "t-mobile", "water bill", "electric", "utility")):
            category = "Bills"
        elif any(k in mn for k in ("transfer", "zelle", "venmo", "paypal", "wire", "ach debit")):
            category = "Transfer"

    return {
        "date": str(t["date"]),
        "description": merchant,
        "merchant_clean": merchant,
        "amount": f"{abs(amount):.2f}",
        "amount_type": amount_type,
        "type": amount_type,
        "category": category,
    }


def create_sandbox_item(institution_id: str = "ins_109508") -> tuple[str, str]:
    """
    Sandbox-only shortcut: create a test item directly via the Plaid API,
    bypassing Plaid Link entirely. Returns (access_token, item_id).

    institution_id "ins_109508" = Chase (sandbox test bank).
    After creation, triggers a transactions refresh so data is ready immediately.
    """
    import time
    client = _get_client()
    req = SandboxPublicTokenCreateRequest(
        institution_id=institution_id,
        initial_products=[Products("transactions")],
    )
    resp = client.sandbox_public_token_create(req)
    public_token = resp["public_token"]
    access_token, item_id = exchange_public_token(public_token)

    # Trigger transactions refresh so data is ready — sandbox needs this
    try:
        client.transactions_refresh(TransactionsRefreshRequest(access_token=access_token))
    except Exception:
        pass
    time.sleep(5)  # Give Plaid sandbox a moment to populate transactions

    return access_token, item_id


def fetch_balance(account_id: int) -> str | None:
    """
    Return the current balance for the first account linked to this item,
    formatted as '$17,347.66'. Returns None if unavailable.
    """
    access_token = load_access_token(account_id)
    if not access_token:
        return None
    client = _get_client()
    resp = client.accounts_balance_get(AccountsBalanceGetRequest(access_token=access_token))
    accounts = resp["accounts"]
    if not accounts:
        return None
    bal = accounts[0]["balances"].get("current")
    return f"${bal:,.2f}" if bal is not None else None
