/**
 * FIFO DOM Discovery Script
 *
 * Paste this in the browser DevTools console while on your bank's
 * transactions page. It will print the exact selectors FIFO needs
 * to fill into the bank's fetcher config.
 *
 * How to use:
 * 1. Log in to your bank and navigate to the transactions / activity page
 * 2. Open DevTools (Cmd+Option+I on Mac)
 * 3. Paste this entire script into the Console tab and hit Enter
 * 4. Copy the output into the bank's fetcher config JSON
 */

(function() {

  // ── Try to find transaction rows ──────────────────────────────────
  // Covers common patterns across BofA, Capital One, Discover, Chase, etc.
  const rowCandidates = [
    // data-testid patterns (React-based UIs)
    "[data-testid='purchase-list-item']",
    "[data-testid='txn-row']",
    "[data-testid='transaction-row']",
    "[data-testid='transaction-item']",
    "[data-testid='activity-row']",
    // Angular / BEM patterns (Capital One, Discover)
    "c1-ease-row.c1-ease-table__row",
    "tr[class*='activity-row']",
    "tr[class*='transaction']",
    "li[class*='TransactionRow']",
    "li[class*='transaction']",
    "li[class*='purchase']",
    "div[class*='TransactionItem']",
    "div[class*='transaction-item']",
    "div[class*='transaction-row']",
    // Discover-specific guesses
    "[data-discovery='transaction']",
    "section[class*='transaction'] li",
    "ul[class*='transaction'] li",
    // Generic ARIA
    "[role='row']",
  ];

  let bestSelector = null;
  let bestCount = 0;
  console.log("── Scanning for transaction row selectors ──");
  for (const sel of rowCandidates) {
    try {
      const els = document.querySelectorAll(sel);
      if (els.length > 0) {
        const marker = els.length > bestCount ? "★" : " ";
        console.log(`${marker} ${sel} → ${els.length} element(s)`);
        if (els.length > bestCount) { bestCount = els.length; bestSelector = sel; }
      }
    } catch(e) {}
  }
  console.log(`\n🏆 Best row selector: ${bestSelector} (${bestCount} rows)\n`);

  // ── Inspect first row for column selectors ────────────────────────
  if (bestSelector) {
    const firstRow = document.querySelector(bestSelector);
    console.log("── First row HTML (first 800 chars) ──");
    console.log(firstRow.outerHTML.substring(0, 800));

    console.log("\n── data-testid attributes in first row ──");
    firstRow.querySelectorAll("[data-testid]").forEach(el => {
      console.log(`  [data-testid="${el.dataset.testid}"] → "${el.textContent.trim().substring(0, 60)}"`);
    });

    console.log("\n── Class names matching date/amount/merchant/description ──");
    firstRow.querySelectorAll("*").forEach(el => {
      const cls = (typeof el.className === "string") ? el.className : "";
      if (/date|amount|merchant|description|price|value|detail|name|trans/i.test(cls)) {
        const text = el.textContent.trim().substring(0, 60).replace(/\s+/g, " ");
        if (text) console.log(`  .${cls.trim().split(/\s+/).join(".")} → "${text}"`);
      }
    });
  }

  // ── Try to find "load more" / "see more" button ───────────────────
  const moreCandidates = [
    "[data-testid='see-more-button']",
    "[data-testid='load-more']",
    "[data-testid='show-more']",
    "button[class*='see-more']",
    "button[class*='load-more']",
    "button[class*='show-more']",
    "button[class*='ViewMore']",
    "button[class*='view-more']",
    "button[class*='load-next']",
    "a[class*='view-more']",
    "button[aria-label*='more transaction']",
    "button[aria-label*='load more']",
    "button[aria-label*='show more']",
    // Capital One
    "button.c1-ease-card-transactions-view__load-next-statement--button",
  ];
  console.log("\n── Scanning for load-more button ──");
  for (const sel of moreCandidates) {
    try {
      const el = document.querySelector(sel);
      if (el) console.log(`  ✓ ${sel} → "${el.textContent.trim()}"`);
    } catch(e) {}
  }

  // ── Summary ───────────────────────────────────────────────────────
  console.log("\n── Paste these into your fetcher config ──");
  console.log(JSON.stringify({
    "row_selector": bestSelector || "NOT FOUND — inspect HTML manually",
    "view_more_selector": "TODO — check load-more scan above",
    "columns": {
      "date":        "TODO",
      "description": "TODO",
      "amount":      "TODO"
    }
  }, null, 2));

})();
