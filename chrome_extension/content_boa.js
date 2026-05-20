// FIFO Sync — Bank of America Content Script
// Injected into bankofamerica.com pages.
// Strategy: background opens last_url (account transactions page directly).
// Waits for tr.activity-row rows, handles "View More" pagination, reports back.

(function () {
  const url = window.location.href;
  log("BOA content script loaded on:", url);

  // ── Not logged in ──────────────────────────────────────────────────────
  if (url.includes("sign-in") || url.includes("signin") || url.includes("login")) {
    chrome.runtime.sendMessage({
      type: "SCRAPE_RESULT",
      payload: { error: "NOT_LOGGED_IN" },
    });
    return;
  }

  // ── Must be on a page with transaction rows ────────────────────────────
  // If on myaccounts overview (not a specific account), report error.
  if (url.includes("myaccounts") && !url.includes("account-details") && !url.includes("activity")) {
    // We're on the accounts list — can't scrape from here
    sendError("Please do a manual login from FIFO and click your account first — the extension needs the direct account transactions URL.");
    return;
  }

  // ── Wait for transaction rows ──────────────────────────────────────────
  log("Waiting for BOA transaction rows…");

  waitForElement(() => document.querySelector("tr.activity-row"), 45_000)
    .then(async () => {
      log("Transaction rows found — loading all and scraping…");
      await loadAllTransactions();
      const transactions = scrapeTransactions();
      const endingBalance = scrapeBalance();
      log(`Scraped ${transactions.length} transactions.`);
      chrome.runtime.sendMessage({
        type: "SCRAPE_RESULT",
        payload: { transactions, endingBalance },
      });
    })
    .catch(() => {
      sendError("No transaction rows found on this BOA page — please do a manual login from FIFO first.");
    });
})();

// ── Click "View More" until all transactions are loaded ───────────────────
async function loadAllTransactions() {
  const MAX_CLICKS = 30;
  for (let i = 0; i < MAX_CLICKS; i++) {
    const viewMore = findViewMore();
    if (!viewMore) break;
    const countBefore = document.querySelectorAll("tr.activity-row").length;
    viewMore.click();
    try {
      await waitForMoreRows(countBefore, 10_000);
    } catch (_) {
      break; // no new rows appeared — done
    }
    await sleep(500); // brief settle
  }
}

// ── Scrape all visible transaction rows ───────────────────────────────────
function scrapeTransactions() {
  const rows = document.querySelectorAll("tr.activity-row");
  const results = [];

  rows.forEach((row) => {
    try {
      const dateEl   = row.querySelector("td:nth-child(1)");
      const descEl   = row.querySelector("td:nth-child(2)");
      const typeEl   = row.querySelector("td:nth-child(3)");
      const amtEl    = row.querySelector("td:nth-child(4)");
      const balEl    = row.querySelector("td:nth-child(5)");

      const dateText = dateEl?.innerText?.trim() || "";
      const descText = descEl?.innerText?.trim().split("\n")[0] || "";
      const typeText = typeEl?.innerText?.trim().toLowerCase() || "";
      const amtText  = amtEl?.innerText?.trim() || "";
      const balText  = balEl?.innerText?.trim() || "";

      if (!descText && !amtText) return;

      // BOA shows type column: "debit" / "credit" or infers from amount sign
      let amountType = typeText.includes("credit") ? "credit" : "debit";
      const amtNum = parseFloat(amtText.replace(/[$,()-]/g, "")) || 0;
      if (amtText.includes("(") || amtText.includes("-")) amountType = "debit";

      results.push({
        date:        normalizeDate(dateText),
        description: descText,
        amount:      String(Math.abs(amtNum)),
        amount_type: amountType,
        raw_amount:  amtText,
        balance:     balText,
        pending:     dateText.toLowerCase().includes("pending") || row.classList.contains("pending"),
      });
    } catch (_) {
      // skip bad rows
    }
  });

  return results;
}

// ── Scrape ending (current) balance ───────────────────────────────────────
function scrapeBalance() {
  const candidates = [
    ".available-balance-amount",
    ".current-balance-amount",
    "[data-testid*='balance']",
    ".account-summary-balance",
    ".balance-amount",
    "[class*='AccountBalance']",
  ];
  for (const sel of candidates) {
    const el = document.querySelector(sel);
    if (el) {
      const txt = el.innerText?.trim();
      if (txt && /[\d,]+/.test(txt)) return txt;
    }
  }
  return "";
}

// ── Find "View More Transactions" link/button ─────────────────────────────
function findViewMore() {
  const els = document.querySelectorAll("a, button");
  for (const el of els) {
    const txt = el.innerText?.trim().toLowerCase();
    if (
      txt === "view more transactions" ||
      txt === "view more" ||
      txt === "show more" ||
      txt === "load more"
    ) return el;
  }
  return null;
}

// ── Wait for row count to increase ───────────────────────────────────────
function waitForMoreRows(currentCount, timeoutMs) {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    const check = setInterval(() => {
      const rows = document.querySelectorAll("tr.activity-row");
      if (rows.length > currentCount) {
        clearInterval(check);
        resolve();
      } else if (Date.now() - start > timeoutMs) {
        clearInterval(check);
        reject(new Error("Timed out waiting for more rows"));
      }
    }, 400);
  });
}

// ── Helpers ───────────────────────────────────────────────────────────────
function normalizeDate(raw) {
  if (!raw || raw.toLowerCase().includes("pending")) return "Pending";
  try {
    const d = new Date(raw);
    if (!isNaN(d)) return d.toISOString().split("T")[0];
    const withYear = new Date(`${raw} ${new Date().getFullYear()}`);
    if (!isNaN(withYear)) return withYear.toISOString().split("T")[0];
  } catch (_) {}
  return raw;
}

function waitForElement(finder, timeoutMs) {
  return new Promise((resolve, reject) => {
    const el = finder();
    if (el) { resolve(el); return; }
    const interval = 400;
    let elapsed = 0;
    const timer = setInterval(() => {
      elapsed += interval;
      const found = finder();
      if (found) {
        clearInterval(timer);
        resolve(found);
      } else if (elapsed >= timeoutMs) {
        clearInterval(timer);
        reject(new Error(`Element not found after ${timeoutMs}ms`));
      }
    }, interval);
  });
}

function sleep(ms) {
  return new Promise(r => setTimeout(r, ms));
}

function sendError(msg) {
  log("ERROR:", msg);
  chrome.runtime.sendMessage({ type: "SCRAPE_RESULT", payload: { error: msg } });
}

function log(...args) {
  console.log("[FIFO BOA]", ...args);
}
