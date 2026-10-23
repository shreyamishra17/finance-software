// FIFO Sync — ICICI Bank Content Script
// Injected into retailnetbanking.icici.bank.in pages.
// Strategy: the background opens last_url (the detailed statement page directly).
// This script waits for transaction rows, handles pagination, and reports back.

(function () {
  const url = window.location.href;
  log("ICICI content script loaded on:", url);

  // ── Not logged in ──────────────────────────────────────────────────────
  if (url.includes("login-page") || url.includes("/login") || url.includes("logout")) {
    chrome.runtime.sendMessage({
      type: "SCRAPE_RESULT",
      payload: { error: "NOT_LOGGED_IN" },
    });
    return;
  }

  // ── Any ICICI page — try to scrape transaction rows ───────────────────
  // If we land on the right page (detailed statement), rows will appear.
  // If we land on overview (no last_url set), report error so user does manual login.
  log("Waiting for ICICI transaction rows…");

  waitForElement(() => document.querySelector("tr.transfer:not(.trans_space)"), 45_000)
    .then(async () => {
      log("Transaction rows found — scraping all pages…");
      const transactions = await scrapeAllPages();
      const endingBalance = scrapeBalance();
      log(`Total scraped: ${transactions.length} transactions`);
      chrome.runtime.sendMessage({
        type: "SCRAPE_RESULT",
        payload: { transactions, endingBalance },
      });
    })
    .catch(() => {
      // Rows never appeared — probably on overview/wrong page
      sendError("No transaction rows found — please do a manual login from FIFO to navigate to the statement page first.");
    });
})();

// ── Scrape all pages (handles ICICI pagination) ───────────────────────────
async function scrapeAllPages() {
  let allTxns = [];
  let page = 1;
  const MAX_PAGES = 50;

  while (page <= MAX_PAGES) {
    const rows = scrapeCurrentPage();
    log(`Page ${page}: scraped ${rows.length} rows`);
    allTxns = allTxns.concat(rows);

    // Find the "next page" button — the > button right of "Page N of N" text
    const nextBtn = findNextPageButton();
    if (!nextBtn) {
      log("No next page button — done.");
      break;
    }

    nextBtn.click();
    page++;

    // Wait for new rows to load (ICICI re-renders the tbody)
    try {
      await waitForNewRows(allTxns.length, 15_000);
    } catch (_) {
      log("Timed out waiting for next page — stopping.");
      break;
    }
  }

  return allTxns;
}

// ── Scrape rows on the current page ──────────────────────────────────────
function scrapeCurrentPage() {
  const rows = document.querySelectorAll("tr.transfer:not(.trans_space)");
  const results = [];

  // Build a date map: walk the DOM to find date headings above rows
  // ICICI wraps groups of rows under a date header element
  rows.forEach((row) => {
    try {
      const date = extractRowDate(row);
      const descEl   = row.querySelector("td.remarks-details p.name, td.remarks-details p.remark-pos");
      const creditEl = row.querySelector("td.credit p, td.cr-mob p");
      const debitEl  = row.querySelector("td.debit p");
      const balEl    = row.querySelector("td.balance p");

      const descText   = descEl?.innerText?.trim().split("\n")[0] || "";
      const creditText = creditEl?.innerText?.trim() || "";
      const debitText  = debitEl?.innerText?.trim() || "";
      const balText    = balEl?.innerText?.trim() || "";

      if (!descText) return; // skip empty rows

      // Determine amount and type
      let amount = "";
      let amountType = "";
      if (creditText && creditText !== "-" && creditText !== "0.00") {
        amount = parseAmount(creditText);
        amountType = "credit";
      } else if (debitText && debitText !== "-" && debitText !== "0.00") {
        amount = parseAmount(debitText);
        amountType = "debit";
      }

      results.push({
        date:        date,
        description: descText,
        amount:      amount,
        amount_type: amountType,
        raw_amount:  creditText || debitText,
        balance:     balText,
        pending:     false,
      });
    } catch (e) {
      // skip bad rows
    }
  });

  return results;
}

// ── Extract date for a row (walks up DOM to find nearest date heading) ────
function extractRowDate(row) {
  // ICICI groups rows under a date header. Walk up to the transaction group div.
  // Date structure: div.transaction > div.date > p.date-1 (day) + p.date-2 (month/year)
  // Also look for div.date-transaction-top for the year.
  try {
    let el = row.parentElement;
    let attempts = 0;
    while (el && attempts < 10) {
      // Look for a date element as a sibling or child of the group
      const dateGroup = el.querySelector("div.date, div[class*='date']");
      if (dateGroup) {
        const day   = dateGroup.querySelector("p.date-1, span.date-1")?.innerText?.trim();
        const month = dateGroup.querySelector("p.date-2, span.date-2")?.innerText?.trim();
        if (day && month) {
          const year = new Date().getFullYear();
          return normalizeDate(`${day} ${month} ${year}`);
        }
        const full = dateGroup.innerText?.trim();
        if (full) return normalizeDate(full);
      }
      // Look for a plain date text in a sibling row/header
      const headerEl = el.previousElementSibling;
      if (headerEl) {
        const txt = headerEl.innerText?.trim();
        if (txt && /\d{1,2}[\s/-]\w+/i.test(txt)) return normalizeDate(txt);
      }
      el = el.parentElement;
      attempts++;
    }
  } catch (_) {}
  return "";
}

// ── Find the "next page" > button ────────────────────────────────────────
function findNextPageButton() {
  // ICICI shows "Page N of N" text with a > arrow button right after it
  const allBtns = Array.from(document.querySelectorAll("button, a, span, div"));
  for (const el of allBtns) {
    const txt = el.innerText?.trim();
    if (!txt) continue;
    // Look for an element that IS the > button (just ">")
    if (txt === ">" || txt === "›" || txt === "❯") {
      if (!el.disabled && el.getAttribute("aria-disabled") !== "true") return el;
    }
  }
  // Fallback: find the pagination area and look for the last button
  const pageInfo = allBtns.find(el => /page\s+\d+\s+of\s+\d+/i.test(el.innerText));
  if (pageInfo) {
    let sibling = pageInfo.nextElementSibling;
    while (sibling) {
      if ((sibling.tagName === "BUTTON" || sibling.tagName === "A") && !sibling.disabled) {
        return sibling;
      }
      sibling = sibling.nextElementSibling;
    }
  }
  return null;
}

// ── Wait for new rows to appear (after pagination click) ─────────────────
function waitForNewRows(currentCount, timeoutMs) {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    const check = setInterval(() => {
      const rows = document.querySelectorAll("tr.transfer:not(.trans_space)");
      if (rows.length !== currentCount) {
        clearInterval(check);
        resolve();
      } else if (Date.now() - start > timeoutMs) {
        clearInterval(check);
        reject(new Error("Timed out"));
      }
    }, 400);
  });
}

// ── Scrape ending balance ─────────────────────────────────────────────────
function scrapeBalance() {
  const candidates = [
    ".balance-amount", ".current-balance", "[class*='balance']",
    ".account-balance", "[data-testid*='balance']",
  ];
  for (const sel of candidates) {
    const el = document.querySelector(sel);
    if (el) {
      const txt = el.innerText?.trim();
      if (txt && /[\d,]+/.test(txt)) return txt;
    }
  }
  // Last resort: use the balance column of the first (most recent) transaction row
  const firstRow = document.querySelector("tr.transfer:not(.trans_space) td.balance p");
  return firstRow?.innerText?.trim() || "";
}

// ── Helpers ───────────────────────────────────────────────────────────────
function parseAmount(raw) {
  if (!raw) return "";
  return raw.replace(/[₹$€£¥,\s]/g, "").trim();
}

function normalizeDate(raw) {
  if (!raw) return "";
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

function sendError(msg) {
  log("ERROR:", msg);
  chrome.runtime.sendMessage({ type: "SCRAPE_RESULT", payload: { error: msg } });
}

function log(...args) {
  console.log("[FIFO ICICI]", ...args);
}
