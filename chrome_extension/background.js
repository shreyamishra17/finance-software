/**
 * FIFO Bank Connector — Background Service Worker
 *
 * Flow:
 *  1. FIFO web app fires window.postMessage({type:'FIFO_SMART_CONNECT', bank:'boa'})
 *  2. fifo_bridge.js (content script on FIFO page) relays to chrome.runtime.sendMessage
 *  3. This service worker opens the bank's login page in a new tab
 *  4. User logs in normally — no bot detection because it's their real browser
 *  5. chrome.tabs.onUpdated detects when the user reaches the accounts/transactions page
 *  6. Service worker injects the scraper function into the BofA tab
 *  7. Scraper reads the DOM, returns transactions array
 *  8. Service worker POSTs to /api/smart-connect/extension-ingest on the FIFO backend
 *  9. Success/error is forwarded back to the FIFO tab content script → FIFO web app
 */

// Map of bank_key → login URL
const BANK_LOGIN_URLS = {
  boa: "https://www.bankofamerica.com/",
};

// Map of bank_key → URL pattern that indicates the user has logged in
const BANK_LOGGED_IN_PATTERNS = {
  boa: /bankofamerica\.com\/myaccounts/i,
};

// Map of bank_key → URL pattern for the transactions/activity page
const BANK_TRANSACTIONS_PATTERNS = {
  boa: /bankofamerica\.com\/(online-banking|accounts)\/(overview|details|activity)/i,
};

// In-memory store: boaTabId → { bank, apiBase, fifoTabId, status }
const activeSyncs = {};

// ── Message handler ──────────────────────────────────────────────────────────

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === "FIFO_PING") {
    // Used by FIFO page to detect if extension is installed
    sendResponse({ installed: true, version: chrome.runtime.getManifest().version });
    return true;
  }

  if (msg.type === "START_BANK_SYNC") {
    const { bank, apiBase } = msg;
    const fifoTabId = sender.tab?.id;
    console.log(`[FIFO] Starting sync for bank=${bank} fifoTab=${fifoTabId}`);
    startBankSync(bank, apiBase, fifoTabId);
    sendResponse({ ok: true });
    return true;
  }
});

// ── Start sync ───────────────────────────────────────────────────────────────

async function startBankSync(bank, apiBase, fifoTabId) {
  const loginUrl = BANK_LOGIN_URLS[bank];
  if (!loginUrl) {
    notifyFifoTab(fifoTabId, "error", `Unsupported bank: ${bank}`);
    return;
  }

  try {
    const tab = await chrome.tabs.create({ url: loginUrl, active: true });
    activeSyncs[tab.id] = { bank, apiBase, fifoTabId, status: "waiting_login" };
    notifyFifoTab(fifoTabId, "logging_in", "Opening your bank in a new tab — please log in.");
  } catch (err) {
    notifyFifoTab(fifoTabId, "error", `Could not open bank tab: ${err.message}`);
  }
}

// ── Tab update listener ──────────────────────────────────────────────────────

chrome.tabs.onUpdated.addListener(async (tabId, changeInfo, tab) => {
  const ctx = activeSyncs[tabId];
  if (!ctx || changeInfo.status !== "complete" || !tab.url) return;

  const { bank, apiBase, fifoTabId } = ctx;

  // Step A: user just landed on the accounts overview after login
  if (BANK_LOGGED_IN_PATTERNS[bank]?.test(tab.url) && ctx.status === "waiting_login") {
    ctx.status = "navigating";
    notifyFifoTab(fifoTabId, "scraping", "Logged in! Reading your transactions…");

    try {
      // Inject the banner so user knows what's happening
      await chrome.scripting.executeScript({
        target: { tabId },
        func: injectFifoBanner,
        args: ["FIFO is reading your transactions — please don't close this tab."],
      });

      // Navigate to the transactions page for BofA
      if (bank === "boa") {
        await navigateToBoaTransactions(tabId, tab.url);
      }
    } catch (err) {
      console.error("[FIFO] Navigation error:", err);
      handleScrapeResult(tabId, null, err.message);
    }
    return;
  }

  // Step B: we've navigated to the activity/transactions page
  if (BANK_TRANSACTIONS_PATTERNS[bank]?.test(tab.url) && ctx.status === "navigating") {
    ctx.status = "scraping";
    // Small delay so the page fully renders
    await sleep(2500);
    try {
      const results = await chrome.scripting.executeScript({
        target: { tabId },
        func: scrapeBoaTransactions,
      });
      const transactions = results?.[0]?.result ?? [];
      handleScrapeResult(tabId, transactions, null);
    } catch (err) {
      console.error("[FIFO] Scrape error:", err);
      handleScrapeResult(tabId, null, err.message);
    }
  }
});

// ── Navigate to BofA transactions page ──────────────────────────────────────

async function navigateToBoaTransactions(tabId, currentUrl) {
  // Try to click the first checking/savings account link, then navigate
  // to the activity page.  We do this via scripting.executeScript so we
  // stay in the same tab and preserve the session.
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    func: clickFirstBoaAccount,
  });

  const accountUrl = results?.[0]?.result;
  if (accountUrl) {
    await chrome.tabs.update(tabId, { url: accountUrl });
  }
  // If we couldn't find a link the tab.onUpdated listener for the
  // transactions pattern will still fire once the user clicks manually.
}

// ── Injected functions (run inside the bank tab) ─────────────────────────────

/** Injected into BofA accounts overview — returns the href of the first account link. */
function clickFirstBoaAccount() {
  // BofA account links on myaccounts page
  const selectors = [
    "a.AccountItem",
    "a[href*='/online-banking/accounts/']",
    "a[href*='/accounts/overview']",
    "a.account-name",
    ".AccountListItem a",
    "a[href*='account-summary']",
  ];
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (el?.href) return el.href;
  }
  // Last resort: any link that mentions 'account' in its href
  const fallback = [...document.querySelectorAll("a[href]")].find(
    (a) => /account/i.test(a.href) && !/sign-out|logout/i.test(a.href)
  );
  return fallback?.href ?? null;
}

/** Injected into BofA transactions page — reads rows and returns transaction array. */
function scrapeBoaTransactions() {
  const rows = document.querySelectorAll("tr.activity-row, tr[data-transaction-id], .transaction-row");
  if (!rows.length) {
    // Try the newer BofA layout
    const items = document.querySelectorAll("[class*='transaction-item'], [class*='activityRow']");
    if (!items.length) return [];

    return [...items].map((item) => {
      const text = (sel) => item.querySelector(sel)?.textContent?.trim() ?? "";
      return {
        date:        text("[class*='date'], [class*='Date']"),
        description: text("[class*='description'], [class*='Description'], [class*='merchant']"),
        amount:      text("[class*='amount'], [class*='Amount']"),
        balance:     text("[class*='balance'], [class*='Balance']"),
        type:        text("[class*='type'], [class*='Type']"),
      };
    }).filter((t) => t.date || t.description);
  }

  return [...rows].map((row) => {
    const cell = (n) => row.querySelector(`td:nth-child(${n})`)?.textContent?.trim() ?? "";
    // BofA classic layout: date | description | type (debit/credit) | amount | balance
    return {
      date:        cell(1),
      description: cell(2),
      type:        cell(3),
      amount:      cell(4),
      balance:     cell(5),
    };
  }).filter((t) => t.date || t.description);
}

/** Injected to show the user a non-intrusive status banner inside the bank tab. */
function injectFifoBanner(message) {
  const existing = document.getElementById("__fifo_banner__");
  if (existing) { existing.textContent = message; return; }
  const banner = document.createElement("div");
  banner.id = "__fifo_banner__";
  banner.style.cssText = [
    "position:fixed", "top:0", "left:0", "right:0", "z-index:2147483647",
    "background:linear-gradient(90deg,#d4f0e8 0%,#fce4f0 100%)",
    "color:#1f5f55", "font:600 13px/38px sans-serif",
    "text-align:center", "padding:0 16px", "pointer-events:none",
    "border-bottom:1.5px solid rgba(31,95,85,.15)",
    "box-shadow:0 2px 12px rgba(31,95,85,.08)",
    "letter-spacing:.01em",
  ].join(";");
  banner.textContent = message;
  document.body.prepend(banner);
}

// ── Handle scrape result ─────────────────────────────────────────────────────

async function handleScrapeResult(tabId, transactions, errorMsg) {
  const ctx = activeSyncs[tabId];
  if (!ctx) return;

  const { bank, apiBase, fifoTabId } = ctx;
  delete activeSyncs[tabId];

  if (errorMsg || !transactions?.length) {
    notifyFifoTab(fifoTabId, "error", errorMsg || "No transactions found on the page.");
    // Close the bank tab after a short delay so user can see what happened
    setTimeout(() => chrome.tabs.remove(tabId).catch(() => {}), 3000);
    return;
  }

  // Update banner in bank tab
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: injectFifoBanner,
      args: [`FIFO found ${transactions.length} transactions — saving to your account…`],
    });
  } catch (_) {}

  notifyFifoTab(fifoTabId, "saving", `Found ${transactions.length} transactions — saving…`);

  try {
    const resp = await fetch(`${apiBase}/api/smart-connect/extension-ingest`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ bank_key: bank, transactions }),
    });

    if (!resp.ok) {
      const err = await resp.text();
      throw new Error(`Server error ${resp.status}: ${err}`);
    }

    const data = await resp.json();
    notifyFifoTab(fifoTabId, "done", null, data);

    // Close the bank tab
    setTimeout(() => chrome.tabs.remove(tabId).catch(() => {}), 1500);
  } catch (err) {
    console.error("[FIFO] Ingest error:", err);
    notifyFifoTab(fifoTabId, "error", `Could not save transactions: ${err.message}`);
    setTimeout(() => chrome.tabs.remove(tabId).catch(() => {}), 3000);
  }
}

// ── Notify FIFO tab ───────────────────────────────────────────────────────────

function notifyFifoTab(fifoTabId, status, message, data = null) {
  if (!fifoTabId) return;
  chrome.tabs.sendMessage(fifoTabId, {
    type:    "FIFO_SYNC_STATUS",
    status,
    message,
    data,
  }).catch(() => {
    // Tab may have been closed; ignore
  });
}

// ── Utility ───────────────────────────────────────────────────────────────────

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
