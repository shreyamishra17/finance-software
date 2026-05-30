// FIFO Sync — Universal Content Script
// Works on ALL supported bank pages without any code changes.
// Fetches scraping instructions from the FIFO backend, then executes them.
// To add a new bank: update the backend config — no extension update needed.

(function () {
  // No same-URL guard anymore. Background.js explicitly re-injects content.js
  // every time the user clicks Sync Now (or on every page navigation). If we
  // bailed out because we'd already run on this URL, the user would click
  // Sync Now and nothing would happen — exactly the bug that was eating 180s
  // of background.js's wait window.
  const domain = window.location.hostname.toLowerCase();
  const url    = window.location.href;
  log("Universal content script loaded on:", domain, url);

  // Ask background service worker to fetch scrape config (background has network access)
  chrome.runtime.sendMessage({ type: "GET_SCRAPE_CONFIG", domain }, async (response) => {
    if (chrome.runtime.lastError || !response || response.error) {
      // Domain not supported or backend unreachable — silently exit
      log("No config available:", chrome.runtime.lastError?.message || response?.error);
      return;
    }
    log("Got scrape config for", response.config.bank_key);
    await runScrape(response.config, url);
  });
})();

// ── Main scrape runner ────────────────────────────────────────────────────
async function runScrape(config, currentUrl) {
  // ── 0. Hard bail on known error pages ────────────────────────────────
  // BoA returns accounts-overview.go?msgId=MSGOBOError when it flags a
  // session for anti-abuse. Don't touch the DOM there — any click would
  // just trigger another error redirect and dig the hole deeper.
  if (Array.isArray(config.skip_on_url_contains)) {
    for (const marker of config.skip_on_url_contains) {
      if (currentUrl.includes(marker)) {
        log(`Detected error/lockout URL marker "${marker}" — aborting scrape entirely.`);
        showBanner("Bank session blocked — please log out, clear cookies, and try again.", "error");
        return;
      }
    }
  }
  // Also bail if the page text shows a known restriction message — covers
  // cases where the URL hasn't changed yet but BoA already rendered the error.
  const bodyText = (document.body?.innerText || "").slice(0, 5000).toLowerCase();
  if (
    bodyText.includes("your access is restricted") ||
    bodyText.includes("there was a problem processing your request")
  ) {
    log("Detected restriction message in page body — aborting scrape.");
    showBanner("Bank session blocked — please log out, clear cookies, and try again.", "error");
    return;
  }

  showBanner("Logged in — looking for your transactions…");

  // ── 1. Check if not logged in ─────────────────────────────────────────
  // Two-pass detection. URL pattern is the strong signal; if it doesn't match,
  // we fall back to DOM heuristics: presence of password inputs or known
  // session-expired text. This catches cases like ICICI where the bank
  // silently redirects to a login page that isn't in our URL pattern list.
  const urlSaysLogin = config.login_check && config.login_check.some(p => currentUrl.includes(p));
  const domSaysLogin = _domSuggestsLoginPage();
  if (urlSaysLogin || domSaysLogin) {
    const actuallyLoggedIn = config.logged_in_selector &&
      document.querySelector(config.logged_in_selector);
    if (!actuallyLoggedIn) {
      log(`Not logged in — reporting back. (urlMatch=${urlSaysLogin}, domMatch=${domSaysLogin})`);
      removeBanner();
      chrome.runtime.sendMessage({ type: "SCRAPE_RESULT", payload: { error: "NOT_LOGGED_IN" } });
      return;
    }
    log("Login signal detected but logged_in_selector found — proceeding.");
  }

  // ── 2. Run navigation steps ───────────────────────────────────────────
  for (const step of (config.navigate || [])) {
    if (step.on_url && !urlMatchesPattern(currentUrl, step.on_url)) continue;
    // Skip step if a target selector is already present (we're already on the right page)
    if (step.skip_if_selector_present && document.querySelector(step.skip_if_selector_present)) continue;

    if (step.action === "wait_and_click_text") {
      try {
        log(`Navigation: looking for button with texts: ${step.texts.join(", ")}`);
        const el = await waitForAnyText(step.texts, step.timeout || 30000);
        log("Navigation: clicking", el.innerText.trim());
        el.click();
        await sleep(2000);
      } catch (e) {
        log("Navigation step (text) failed (non-fatal):", e.message);
      }
    } else if (step.action === "wait_and_click_selector") {
      try {
        log(`Navigation: waiting for selector: ${step.selector}`);
        showBanner("Opening your account…");
        const el = await waitForElement(() => document.querySelector(step.selector), step.timeout || 30000);
        log("Navigation: clicking", el.innerText?.trim() || el.href);
        // Some BoA links navigate via an in-page router; a plain .click() can
        // be swallowed if the element isn't fully wired. dispatch a real
        // MouseEvent so the framework's listeners fire.
        el.scrollIntoView({ block: "center" });
        el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
        // Page will navigate — this content script instance ends here.
        // A fresh instance will run on the new page and pick up from step 1.
        return;
      } catch (e) {
        // Auto-click failed to find a match within the timeout. Don't error
        // out — show the manual-click hint and let the user navigate
        // themselves. background's navListener will re-run content.js on the
        // next page and pick up the rows from there.
        log("Navigation step (selector) failed — falling back to manual hint:", e.message);
        if (config.first_connect_hint) {
          showBanner(config.first_connect_hint);
        } else {
          showBanner("Click your account to continue.");
        }
        return;
      }
    }
  }

  // ── 3. Wait for ready_url_pattern if specified ────────────────────────
  if (config.ready_url_pattern) {
    try {
      await waitForUrlPattern(config.ready_url_pattern, config.ready_timeout || 60000);
      log("URL pattern matched:", config.ready_url_pattern);
    } catch (e) {
      removeBanner();
      sendError(`Timed out waiting for page to navigate: ${e.message}`);
      return;
    }
  }

  // ── 4. Wait for transaction rows to appear ────────────────────────────
  // When first_connect_hint is set, we're waiting for the user to click an
  // account from an overview page. Keep the banner up indefinitely and
  // surface a friendlier "still waiting" message after each timeout window —
  // background.js's outer timeout (10 min for first-connect) will eventually
  // give up. For non-first-connect (background sync), behave as before.
  const isFirstConnect = !!config.first_connect_hint;
  showBanner(config.first_connect_hint || "Reading your transactions…");
  log("Waiting for rows:", config.ready_selector);

  // Diagnostic: log which selector candidates the page actually has so we
  // can see whether BoA renamed their row class. Run it after a short delay
  // to let the table render.
  setTimeout(() => debugRowSelectors(config.ready_selector), 4000);

  const rowsAppeared = await waitForRowsOrNavigation(
    config.ready_selector,
    config.ready_timeout || 45000,
    isFirstConnect,
    config.first_connect_hint,
  );
  if (!rowsAppeared) {
    // Always tell background.js what happened. The error message intentionally
    // contains "No transaction rows" so background.js's existing
    // ignoreLoginErrors filter recognizes this as a transient first-connect
    // signal (and keeps listening for the next re-injection). In sync-now
    // mode, background propagates it as a clear UI error.
    removeBanner();
    sendError(
      `No transaction rows found on this page. ` +
      `Open your account's activity page in the bank tab, then click Sync Now again.`
    );
    return;
  }

  // ── 5. Load all pages ─────────────────────────────────────────────────
  const startCount = document.querySelectorAll(config.rows.selector).length;
  log(`Rows visible before pagination: ${startCount}`);
  showBanner(`Loading all transactions… (${startCount} so far)`);
  await loadAllPages(config);
  const afterCount = document.querySelectorAll(config.rows.selector).length;
  log(`Rows visible after pagination: ${afterCount}`);

  // ── 6. Scrape rows ────────────────────────────────────────────────────
  const transactions = scrapeRows(config);
  const endingBalance = scrapeBalance(config);

  log(`Scraped ${transactions.length} transactions, balance: ${endingBalance}`);
  showBanner(`✅ Found ${transactions.length} transactions — saving to FIFO…`, "success");
  setTimeout(removeBanner, 3000);

  chrome.runtime.sendMessage({
    type: "SCRAPE_RESULT",
    payload: { transactions, endingBalance, currentUrl: window.location.href },
  });
}

// ── Load all pages before scraping ───────────────────────────────────────
async function loadAllPages(config) {
  if (!config.pagination) { log("No pagination config — skipping"); return; }
  const { type, button_texts, page_info_pattern } = config.pagination;
  const MAX = 50;

  for (let i = 0; i < MAX; i++) {
    let btn = null;
    if (type === "view_more_button")  btn = findButtonByTexts(button_texts);
    else if (type === "next_button")  btn = findNextPageButton(button_texts, page_info_pattern);

    if (!btn) {
      log(`Pagination: no "${(button_texts||[]).join("/")}" button found at iter ${i} — done.`);
      break;
    }

    const countBefore = document.querySelectorAll(config.rows.selector).length;
    log(`Pagination iter ${i}: clicking "${(btn.innerText||"").trim().slice(0,40)}" (rows=${countBefore})`);
    btn.scrollIntoView({ block: "center" });
    btn.click();
    try {
      await waitForRowCountChange(config.rows.selector, countBefore, 12000);
      const after = document.querySelectorAll(config.rows.selector).length;
      log(`Pagination iter ${i}: rows ${countBefore} → ${after}`);
      showBanner(`Loading all transactions… (${after} so far)`);
    } catch (_) {
      log(`Pagination iter ${i}: row count did not change in 12s — stopping.`);
      break;
    }
    // Stop when the oldest visible row reaches our cutoff. Two cutoffs apply:
    //   1. last_txn_date — the date of the most recent transaction we've
    //      already stored for this account. Anything older than that is
    //      already in the database. This is the primary cutoff for any sync
    //      after the first one.
    //   2. stop_after_days — a hard limit (default 365) so the very first
    //      sync of a new account doesn't pull infinite history.
    // Whichever cutoff is MORE RECENT wins.
    if (config.rows.date) {
      const oldest = oldestVisibleRowDate(config);
      if (oldest) {
        const cutoffs = [];
        if (config.stop_after_days) {
          const c = new Date();
          c.setDate(c.getDate() - config.stop_after_days);
          cutoffs.push({ name: `${config.stop_after_days}d`, date: c });
        }
        if (config.last_txn_date) {
          const c = new Date(config.last_txn_date);
          if (isFinite(c.valueOf())) cutoffs.push({ name: `last_txn_date(${config.last_txn_date})`, date: c });
        }
        const chosen = cutoffs.sort((a, b) => b.date - a.date)[0]; // most recent cutoff
        if (chosen && oldest < chosen.date) {
          log(`Pagination: oldest row ${oldest.toISOString().slice(0,10)} < cutoff [${chosen.name}] — done.`);
          break;
        }
      }
    }
    await sleep(300);
  }
}

// Find the oldest visible row's date by looking at the last row in the table.
function oldestVisibleRowDate(config) {
  const rows = document.querySelectorAll(config.rows.selector);
  if (!rows.length) return null;
  const last = rows[rows.length - 1];
  const dateText = (last.querySelector(config.rows.date)?.innerText || "").trim();
  if (!dateText) return null;
  const normalized = normalizeDate(dateText);
  const d = new Date(normalized);
  return isNaN(d.getTime()) ? null : d;
}

// ── Scrape all visible rows using config columns ──────────────────────────
function scrapeRows(config) {
  const rows = document.querySelectorAll(config.rows.selector);
  const results = [];
  const r = config.rows;

  rows.forEach((row) => {
    try {
      let date = "", description = "", amount = "", amountType = "", balance = "", rawAmount = "";

      // Description
      if (r.description) description = firstLine(text(row.querySelector(r.description)));

      // Date
      if (r.date) {
        date = normalizeDate(text(row.querySelector(r.date)));
      } else if (r.date_mode === "parent_walk") {
        date = extractDateByParentWalk(row);
      }

      // Amount & type
      if (r.amount_sign === "credit_debit_cols") {
        const creditTxt = text(row.querySelector(r.credit));
        const debitTxt  = text(row.querySelector(r.debit));
        balance         = text(row.querySelector(r.balance));
        if (creditTxt && creditTxt !== "-" && creditTxt !== "0.00") {
          amount = parseAmount(creditTxt); amountType = "credit"; rawAmount = creditTxt;
        } else if (debitTxt && debitTxt !== "-" && debitTxt !== "0.00") {
          amount = parseAmount(debitTxt);  amountType = "debit";  rawAmount = debitTxt;
        }
      } else if (r.amount_sign === "type_column") {
        const typeTxt = text(row.querySelector(r.amount_type)).toLowerCase();
        rawAmount = text(row.querySelector(r.amount));
        amount = parseAmount(rawAmount);
        const desc = (description || "").toLowerCase();
        // Safer default: assume debit. Most transactions on a checking
        // account are spends; defaulting to credit was over-counting income.
        amountType = "debit";
        // Explicit type column wins for unambiguous cases.
        if (/deposit/.test(typeTxt)) {
          amountType = "credit";
        } else if (/debit|withdrawal/.test(typeTxt)) {
          amountType = "debit";
        } else if (/transfer/.test(typeTxt)) {
          // Transfer = ambiguous. Look at description.
          // "payment to" / " to " → money out
          // "payment from" / " from " → money in
          if (/\bfrom\b/.test(desc)) {
            amountType = "credit";
          } else if (/\bto\b/.test(desc)) {
            amountType = "debit";
          }
        }
        // Explicit negative sign in the amount string ALWAYS overrides
        // (BoA mostly hides the sign via CSS, but if it's there, trust it).
        if (rawAmount.includes("(") || rawAmount.trim().startsWith("-") || rawAmount.includes("-$")) {
          amountType = "debit";
        }
        if (r.balance) balance = text(row.querySelector(r.balance));
      } else {
        // positive_is_debit (Capital One, Discover)
        rawAmount = text(row.querySelector(r.amount));
        const num = parseFloat(rawAmount.replace(/[$,₹€£¥()]/g, "")) || 0;
        amount = String(Math.abs(num));
        amountType = num < 0 ? "credit" : "debit";
      }

      if (!description && !amount) return; // skip empty rows

      // Tag the row's pre-balance-delta direction guess so the post-pass below
      // can override it without losing the original. (Diagnostic only.)
      const _preDirGuess = amountType;

      const pending = row.classList.contains("pending") ||
                      (date || "").toLowerCase() === "pending";

      results.push({ date, description, amount, amount_type: amountType, raw_amount: rawAmount, balance, pending });
    } catch (_) {}
  });

  // ── Balance-delta post-pass (the truth-tier signal) ──────────────────────
  // Banks that display a running balance per row let us compute each
  // transaction's direction unambiguously: if my balance went UP, money came
  // in (credit); if it went DOWN, money went out (debit). This trumps every
  // other heuristic (amount-sign parsing, type-column text). Used on BoA,
  // ICICI, and any other bank whose config maps `balance`.
  _normalizeDirectionFromBalance(results);

  return results;
}

function _normalizeDirectionFromBalance(rows) {
  if (!rows || rows.length < 2) return;
  // Parse each balance once. Skip rows without a parseable balance.
  const parsedBalance = rows.map((r) => {
    const s = (r.balance || "").replace(/[$,₹€£¥\s]/g, "").replace(/[()]/g, "-");
    const n = parseFloat(s);
    return isFinite(n) ? n : null;
  });
  // Detect row order — newest-first vs oldest-first — from the first two
  // parsed dates we can read. Most bank pages (BoA, ICICI, Capital One) show
  // newest-first. If we can't tell, assume newest-first.
  const _dateAt = (i) => {
    const d = new Date(rows[i]?.date || "");
    return isFinite(d.valueOf()) ? d : null;
  };
  let newestFirst = true;
  for (let i = 0; i < Math.min(rows.length - 1, 10); i++) {
    const a = _dateAt(i), b = _dateAt(i + 1);
    if (a && b && a.valueOf() !== b.valueOf()) {
      newestFirst = a > b;
      break;
    }
  }
  let flipped = 0, confirmed = 0, untrusted = 0, skipped = 0;
  for (let i = 0; i < rows.length - 1; i++) {
    const newerIdx = newestFirst ? i : i + 1;
    const olderIdx = newestFirst ? i + 1 : i;
    const newerBal = parsedBalance[newerIdx];
    const olderBal = parsedBalance[olderIdx];
    if (newerBal == null || olderBal == null) { skipped++; continue; }
    const delta = newerBal - olderBal;
    if (Math.abs(delta) < 0.01) { skipped++; continue; }
    // CRITICAL: only trust delta when |delta| matches |amount| within $0.01.
    // BoA reorders same-day transactions in its display, so consecutive rows
    // are not always consecutive in time — if the math doesn't validate, two
    // rows aren't truly adjacent and we can't trust the direction.
    const amt = parseFloat(rows[newerIdx].amount);
    if (!isFinite(amt) || Math.abs(Math.abs(delta) - amt) > 0.01) {
      untrusted++;
      continue;
    }
    const truth = delta > 0 ? "credit" : "debit";
    if (rows[newerIdx].amount_type !== truth) {
      rows[newerIdx].amount_type = truth;
      flipped++;
    } else {
      confirmed++;
    }
  }
  log(`Balance-delta normalization: ${flipped} flipped, ${confirmed} confirmed, ${untrusted} untrusted (math didn't validate), ${skipped} skipped`);
}

// ── Scrape ending balance ─────────────────────────────────────────────────
function scrapeBalance(config) {
  for (const sel of (config.balance_selectors || [])) {
    const el = document.querySelector(sel);
    if (el) {
      const t = el.innerText?.trim();
      if (t && /[\d,]+/.test(t)) return t;
    }
  }
  return "";
}

// ── Date extraction by walking up the DOM (ICICI) ─────────────────────────
function extractDateByParentWalk(row) {
  try {
    let el = row.parentElement;
    for (let i = 0; i < 10; i++) {
      if (!el) break;
      const dateEl = el.querySelector("div.date, div[class*='date']");
      if (dateEl) {
        const day   = dateEl.querySelector("p.date-1, span.date-1")?.innerText?.trim();
        const month = dateEl.querySelector("p.date-2, span.date-2")?.innerText?.trim();
        if (day && month) return normalizeDate(`${day} ${month} ${new Date().getFullYear()}`);
        const full = dateEl.innerText?.trim();
        if (full) return normalizeDate(full);
      }
      const prev = el.previousElementSibling;
      if (prev && /\d{1,2}[\s/-]\w+/i.test(prev.innerText || "")) {
        return normalizeDate(prev.innerText.trim());
      }
      el = el.parentElement;
    }
  } catch (_) {}
  return "";
}

// ── Button finders ────────────────────────────────────────────────────────
function findButtonByTexts(texts) {
  const els = document.querySelectorAll("a, button");
  for (const el of els) {
    const t = el.innerText?.trim().toLowerCase();
    if (texts.includes(t)) return el;
  }
  return null;
}

function findNextPageButton(buttonTexts, pageInfoPattern) {
  for (const el of document.querySelectorAll("button, a, span")) {
    const t = el.innerText?.trim();
    if (buttonTexts.includes(t) && !el.disabled && el.getAttribute("aria-disabled") !== "true") return el;
  }
  if (pageInfoPattern) {
    const re = new RegExp(pageInfoPattern, "i");
    const allEls = Array.from(document.querySelectorAll("*"));
    const pageEl = allEls.find(el => re.test(el.innerText || ""));
    if (pageEl) {
      let sib = pageEl.nextElementSibling;
      while (sib) {
        if ((sib.tagName === "BUTTON" || sib.tagName === "A") && !sib.disabled) return sib;
        sib = sib.nextElementSibling;
      }
    }
  }
  return null;
}

// ── Helpers ───────────────────────────────────────────────────────────────
function text(el) { return el?.innerText?.trim() || ""; }
function firstLine(s) { return (s || "").split("\n")[0].trim(); }

function parseAmount(raw) {
  return String(Math.abs(parseFloat((raw || "").replace(/[₹$€£¥,\s()]/g, "")) || 0));
}

function normalizeDate(raw) {
  if (!raw || raw.toLowerCase() === "pending") return "Pending";
  try {
    const d = new Date(raw);
    if (!isNaN(d)) return d.toISOString().split("T")[0];
    const withYear = new Date(`${raw} ${new Date().getFullYear()}`);
    if (!isNaN(withYear)) return withYear.toISOString().split("T")[0];
  } catch (_) {}
  return raw;
}

function urlMatchesPattern(url, pattern) {
  const re = new RegExp(pattern.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*"));
  return re.test(url);
}

function waitForUrlPattern(pattern, timeoutMs) {
  return new Promise((resolve, reject) => {
    if (urlMatchesPattern(window.location.href, pattern)) { resolve(); return; }
    const start = Date.now();
    const t = setInterval(() => {
      if (urlMatchesPattern(window.location.href, pattern)) { clearInterval(t); resolve(); }
      else if (Date.now() - start > timeoutMs) { clearInterval(t); reject(new Error("URL pattern timeout")); }
    }, 300);
  });
}

function waitForElement(finder, timeoutMs) {
  return new Promise((resolve, reject) => {
    const el = finder();
    if (el) { resolve(el); return; }
    const start = Date.now();
    const t = setInterval(() => {
      const found = finder();
      if (found) { clearInterval(t); resolve(found); }
      else if (Date.now() - start > timeoutMs) { clearInterval(t); reject(new Error("Element timeout")); }
    }, 400);
  });
}

function waitForAnyText(texts, timeoutMs) {
  return new Promise((resolve, reject) => {
    const find = () => {
      const lc = texts.map(x => x.toLowerCase());
      for (const el of document.querySelectorAll("a, button")) {
        if (lc.includes(el.innerText?.trim().toLowerCase())) return el;
      }
      return null;
    };
    const found = find();
    if (found) { resolve(found); return; }
    const start = Date.now();
    const t = setInterval(() => {
      const el = find();
      if (el) { clearInterval(t); resolve(el); }
      else if (Date.now() - start > timeoutMs) { clearInterval(t); reject(new Error("Text button timeout")); }
    }, 400);
  });
}

// Wait until `selector` appears OR (first-connect only) keep watching with a
// gentle banner heartbeat until the outer background timeout closes the tab.
// Returns true if rows appeared, false otherwise.
function waitForRowsOrNavigation(selector, timeoutMs, isFirstConnect, hint) {
  return new Promise((resolve) => {
    let resolved = false;
    let elapsed = 0;
    const check = () => document.querySelectorAll(selector).length > 0;
    if (check()) { resolve(true); return; }

    const finish = (ok) => {
      if (resolved) return;
      resolved = true;
      clearInterval(poll);
      observer.disconnect();
      resolve(ok);
    };

    // MutationObserver catches SPA renders that don't fire a navigation event.
    const observer = new MutationObserver(() => { if (check()) finish(true); });
    observer.observe(document.body || document.documentElement, { childList: true, subtree: true });

    const poll = setInterval(() => {
      if (check()) { finish(true); return; }
      elapsed += 1000;
      // For first-connect, refresh the hint every 30s so the banner doesn't look stale.
      if (isFirstConnect && hint && elapsed % 30000 === 0) {
        showBanner(`${hint}  (still waiting…)`);
      }
      // Always time out — even in first-connect mode. The previous behavior
      // (never time out when first_connect_hint was set) meant Sync Now
      // hung forever because it inherits the same config. background.js's
      // navListener re-injects content.js on every navigation, so each
      // user-driven nav restarts the wait with a fresh timeout.
      if (elapsed >= timeoutMs) finish(false);
    }, 1000);
  });
}

function waitForRowCountChange(selector, oldCount, timeoutMs) {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    const t = setInterval(() => {
      if (document.querySelectorAll(selector).length !== oldCount) { clearInterval(t); resolve(); }
      else if (Date.now() - start > timeoutMs) { clearInterval(t); reject(new Error("Row count timeout")); }
    }, 400);
  });
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

// Returns true when the current page looks like a bank login / session-expired
// page based on DOM/text heuristics — independent of URL patterns. Catches
// cases where the bank redirects silently to a login page without changing
// the URL into something matching `login_check` (ICICI is notorious for this).
function _domSuggestsLoginPage() {
  try {
    if (document.querySelector("input[type='password']")) return true;
    const text = (document.body?.innerText || "").slice(0, 8000).toLowerCase();
    const phrases = [
      "session expired",
      "session has expired",
      "session timed out",
      "your session has timed out",
      "please login again",
      "please log in again",
      "logged out due to inactivity",
      "session timeout",
      "user id",       // ICICI's login form
      "customer id",   // common Indian-bank login form
    ];
    // Require at least one phrase hit; "user id"/"customer id" alone is too
    // weak, so we also require a password-y input or login button presence.
    if (phrases.some((p) => text.includes(p))) {
      if (document.querySelector("input[type='password'], button, input[type='submit']")) return true;
    }
  } catch (_) { /* noop */ }
  return false;
}

// Log per-selector hit counts plus a sample of any tr-like rows on the page,
// so we can tell from the console what classes/attrs to add to the config
// when BoA renames their row markup.
function debugRowSelectors(combinedSelector) {
  try {
    const parts = (combinedSelector || "").split(",").map((s) => s.trim()).filter(Boolean);
    const counts = parts.map((sel) => {
      try { return `${sel}=${document.querySelectorAll(sel).length}`; }
      catch { return `${sel}=ERR`; }
    });
    log("Selector probe:", counts.join("  |  "));
    const sampleClasses = new Set();
    const sampleAttrs = new Set();
    document.querySelectorAll("tr, [role='row']").forEach((row, i) => {
      if (i > 20) return;
      if (row.className) sampleClasses.add(row.className);
      for (const a of row.attributes) {
        if (a.name.startsWith("data-")) sampleAttrs.add(a.name);
      }
    });
    log("Sample row classes:", [...sampleClasses].slice(0, 12).join(" || "));
    log("Sample row data-attrs:", [...sampleAttrs].join(", "));
  } catch (e) {
    log("Selector probe failed:", e.message);
  }
}

function sendError(msg) {
  log("ERROR:", msg);
  chrome.runtime.sendMessage({ type: "SCRAPE_RESULT", payload: { error: msg } });
}

function log(...args) { console.log("[FIFO]", ...args); }

// ── Status banner injected into the bank page ─────────────────────────────
function showBanner(message, type = "info") {
  let b = document.getElementById("__fifo_banner");
  if (!b) {
    b = document.createElement("div");
    b.id = "__fifo_banner";
    b.style.cssText = [
      "position:fixed", "top:0", "left:0", "right:0", "z-index:2147483647",
      "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',system-ui,sans-serif",
      "font-size:13px", "font-weight:500", "letter-spacing:0.01em",
      "padding:9px 20px", "display:flex", "align-items:center", "gap:10px",
      "box-shadow:0 1px 0 rgba(0,0,0,0.06),0 4px 20px rgba(20,24,28,0.09)",
      "backdrop-filter:blur(16px)", "-webkit-backdrop-filter:blur(16px)",
      "transition:background 0.35s,color 0.35s,border-color 0.35s",
    ].join(";");
    document.body ? document.body.prepend(b) : document.documentElement.prepend(b);
  }
  const themes = {
    info:    { bg: "linear-gradient(135deg,rgba(255,249,244,0.97) 0%,rgba(238,246,242,0.97) 50%,rgba(238,242,251,0.97) 100%)", color: "#2d4040", border: "rgba(198,222,216,0.8)" },
    success: { bg: "linear-gradient(135deg,rgba(232,247,240,0.98) 0%,rgba(208,240,222,0.98) 100%)", color: "#1a4731", border: "rgba(148,210,172,0.7)" },
    error:   { bg: "linear-gradient(135deg,rgba(254,242,242,0.98) 0%,rgba(250,215,215,0.98) 100%)", color: "#7f1d1d", border: "rgba(220,148,148,0.6)" },
  };
  const t = themes[type] || themes.info;
  b.style.background = t.bg;
  b.style.color = t.color;
  b.style.borderBottom = `1.5px solid ${t.border}`;
  const icon = type === "success" ? "✅" : type === "error" ? "⚠️" : "✦";
  b.innerHTML = `
    <span style="background:rgba(45,64,64,0.09);color:${t.color};font-size:10px;font-weight:700;letter-spacing:0.09em;padding:2px 8px;border-radius:100px;border:1px solid rgba(45,64,64,0.12);flex-shrink:0">FIFO</span>
    <span style="font-size:14px;flex-shrink:0">${icon}</span>
    <span style="opacity:0.88">${message}</span>
  `;
}

function removeBanner() {
  document.getElementById("__fifo_banner")?.remove();
}
