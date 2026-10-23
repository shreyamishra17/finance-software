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
    // Re-entrancy guard: background.js re-injects content.js on page loads, so a
    // single SPA navigation can spawn several copies that all run the navigate
    // flow at once — they click ACCOUNTS / View Statement / Detailed Statement
    // on top of each other and fight. Allow only ONE scrape per document. The
    // flag lives on globalThis (per JS world): a full page navigation creates a
    // fresh world and is correctly allowed to start its own run. The check+set
    // is synchronous (before any await), so two callbacks can't both pass.
    if (globalThis.__fifoScrapeRunning) {
      log("Duplicate injection — a scrape is already running on this document, skipping.");
      return;
    }
    globalThis.__fifoScrapeRunning = true;
    log("Got scrape config for", response.config.bank_key);
    try {
      await runScrape(response.config, url);
    } finally {
      globalThis.__fifoScrapeRunning = false;
    }
  });
})();

// ── Main scrape runner ────────────────────────────────────────────────────
async function runScrape(config, currentUrl) {
  // ── 0. Hard bail on known error pages ────────────────────────────────
  // BoA returns accounts-overview.go?msgId=MSGOBOError when it flags a
  // session for anti-abuse. Don't touch the DOM there — any click would
  // just trigger another error redirect and dig the hole deeper.
  // A marker (URL or body text) is only a REAL block if the page is genuinely
  // unusable. BoA routinely renders a benign "problem processing your request"
  // warning box on a fully functional accounts-overview page (msgId=MSGOBOError)
  // where the real account links are present and clickable. In that case we must
  // NOT bail — we proceed and click the account just like the user would.
  // skip_override_selector names the elements that prove the page is usable.
  const pageIsUsable = !!(config.skip_override_selector &&
    document.querySelector(config.skip_override_selector));

  if (Array.isArray(config.skip_on_url_contains)) {
    for (const marker of config.skip_on_url_contains) {
      if (currentUrl.includes(marker)) {
        if (pageIsUsable) {
          log(`URL marker "${marker}" present but page is usable — treating as benign warning, continuing.`);
          break;
        }
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
    !pageIsUsable &&
    (bodyText.includes("your access is restricted") ||
     bodyText.includes("there was a problem processing your request"))
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
        // ── Loop guard ────────────────────────────────────────────────────
        // If clicking lands us back on the SAME url (e.g. BoA's "Continue"
        // button bouncing to accounts-overview.go?msgId=MSGOBOError), content.js
        // re-runs and we'd click again forever. Track clicks per-URL in
        // sessionStorage (survives same-tab reloads) and bail after 2 attempts.
        const loopKey = "fifo_nav_clicks_" + location.pathname + location.search;
        const priorClicks = parseInt(sessionStorage.getItem(loopKey) || "0", 10);
        if (priorClicks >= 2) {
          log(`Loop guard: already clicked ${priorClicks}x from this URL with no progress — stopping to avoid a refresh loop.`);
          showBanner(config.first_connect_hint || "Please click your account to continue.");
          return;
        }

        log(`Navigation: waiting for selector: ${step.selector}`);
        showBanner("Opening your account…");
        // The selector is deliberately broad (BoA renames classes often), so it
        // matches several decoys alongside the real account link: a "Continue"
        // button, a rewards/special-offer link, a Merrill cross-sell — all share
        // the same redirect.go?source=overview href shape. Rather than blocklist
        // each decoy, SCORE every candidate and click the best one:
        //   • hard-exclude by visible text   (click_skip_text  → e.g. "continue")
        //   • hard-exclude by href substring  (click_skip_href  → "rewards", …)
        //   • positively prefer account links (click_prefer_text → "banking", …)
        //   • positively prefer links whose text has a masked acct number (digits)
        // This positively targets the account link instead of guessing which
        // decoy comes next in DOM order.
        const skipText = (step.click_skip_text || []).map(s => s.toLowerCase());
        const skipHref = (step.click_skip_href || []).map(s => s.toLowerCase());
        const preferText = (step.click_prefer_text || []).map(s => s.toLowerCase());
        const scoreOf = (cand) => {
          const t = (cand.innerText || cand.textContent || "").trim().toLowerCase();
          const href = (cand.getAttribute("href") || cand.href || "").toLowerCase();
          // Hard excludes — never click these.
          if (t && skipText.some(s => t === s || t.includes(s))) return -1;
          if (href && skipHref.some(s => href.includes(s))) return -1;
          let score = 0;
          if (t && preferText.some(s => t.includes(s))) score += 100; // account-type keyword
          if (/\d{3,}/.test(t)) score += 20;                          // masked account number
          if (t) score += 1;                                          // any visible label
          return score;
        };
        const pick = () => {
          const all = [...document.querySelectorAll(step.selector)];
          let best = null, bestScore = 0;
          for (const cand of all) {
            const s = scoreOf(cand);
            // Read-only diagnostic: dump every candidate so the console shows
            // exactly what matched (text + href + score) — no guessing which
            // link won or why.
            const t = (cand.innerText || cand.textContent || "").trim().slice(0, 60);
            const h = (cand.getAttribute("href") || cand.href || "").slice(0, 120);
            log(`  candidate score=${s} text="${t}" href="${h}"`);
            if (s > bestScore) { bestScore = s; best = cand; }
          }
          if (best) log(`Navigation: picked best candidate score=${bestScore} of ${all.length} matches`);
          return best; // null → nothing scored > 0 (all excluded); waitForElement keeps polling
        };
        const el = await waitForElement(pick, step.timeout || 30000);
        log("Navigation: clicking", el.innerText?.trim() || el.href);
        sessionStorage.setItem(loopKey, String(priorClicks + 1));
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
    } else if (step.action === "select_ngselect") {
      // Drive an ng-select dropdown (ICICI's "Select transaction period"). The
      // library does NOT respond to a plain click on its value label — it toggles
      // open on a `mousedown` against its `.ng-select-container`, then renders
      // options in a detached `.ng-dropdown-panel` as `.ng-option` nodes. So:
      // (1) find the right ng-select (by its current value, e.g. "Today"),
      // (2) mousedown its container to open, (3) wait for the panel, (4) click
      // the `.ng-option` whose text matches `value` (e.g. "Last 1 Year").
      try {
        const dispatch = (el, types) => {
          for (const t of types) {
            const Ev = t.startsWith("pointer") && typeof PointerEvent === "function" ? PointerEvent : MouseEvent;
            try { el.dispatchEvent(new Ev(t, { bubbles: true, cancelable: true, view: window })); } catch (_) {}
          }
        };
        const trigWant = (step.trigger_text || "").toLowerCase();
        const selects = [...document.querySelectorAll("ng-select, .ng-select")]
          .filter((s) => s.offsetParent !== null);
        let target = null;
        for (const s of selects) {
          const lbl = (s.querySelector(".ng-value-label")?.innerText || "").trim().toLowerCase();
          if (!trigWant || lbl.includes(trigWant)) { target = s; break; }
        }
        if (!target) target = selects[0];
        if (!target) {
          log(`select_ngselect: no ng-select found (wanted trigger "${step.trigger_text}")`);
        } else {
          const container = target.querySelector(".ng-select-container") || target;
          log("select_ngselect: opening ng-select (current value:",
              JSON.stringify((target.querySelector(".ng-value-label")?.innerText || "").trim()) + ")");
          target.focus && target.focus();
          dispatch(container, ["pointerdown", "mousedown", "mouseup"]);
          const want = (step.value || "").toLowerCase();
          const findOpt = () => [...document.querySelectorAll(
            ".ng-dropdown-panel .ng-option, .ng-option, [role='option']")]
            .find((o) => o.offsetParent !== null &&
                   (o.innerText || o.textContent || "").trim().toLowerCase().includes(want));
          let opt = null;
          for (let i = 0; i < 40 && !opt; i++) { opt = findOpt(); if (!opt) await sleep(150); }
          if (!opt) {
            log(`select_ngselect: option "${step.value}" not found after opening panel`);
            // Diagnostic: list what options DID render.
            const opts = [...document.querySelectorAll(".ng-dropdown-panel .ng-option, .ng-option")]
              .filter((o) => o.offsetParent !== null)
              .map((o) => `"${(o.innerText || "").trim().slice(0, 20)}"`);
            log("select_ngselect: visible ng-options:", opts.join(", ") || "(none — panel never opened)");
          } else {
            log("select_ngselect: clicking option", JSON.stringify((opt.innerText || "").trim().slice(0, 30)));
            opt.scrollIntoView({ block: "center" });
            dispatch(opt, ["pointerdown", "mousedown", "mouseup", "click"]);
          }
        }
        await sleep(step.sleep_after || 1500);
      } catch (e) {
        log("select_ngselect failed (non-fatal):", e.message);
        await sleep(500);
      }
    } else if (step.action === "debug_dropdown") {
      // Diagnostic-only: find the ICICI period control. Scan the whole document
      // for visible elements whose OWN text is a period value (Today / Last 1
      // Month / Last 1 Year / FY 20xx-20xx) and log tag.class for each — that
      // reveals both the closed trigger (shows "Today") and, if open, the option
      // list items, regardless of container nesting. Short output (resists log
      // truncation). Also reports a native <select> if one exists. Never fatal.
      try {
        await sleep(step.sleep_before || 0);
        const tc = (el) => {
          const cls = (el.className || "").toString().trim().replace(/\s+/g, ".");
          const role = el.getAttribute && el.getAttribute("role");
          return el.tagName.toLowerCase() + (cls ? "." + cls : "") + (role ? `[role=${role}]` : "");
        };
        const periodRe = /^(today|last\s+\d+\s+(month|months|year|years)|fy\s*\d{4}\s*-\s*\d{4})$/i;
        const hits = [];
        for (const el of document.querySelectorAll("*")) {
          if (el.offsetParent === null) continue;
          const own = [...el.childNodes].filter((n) => n.nodeType === 3)
            .map((n) => n.textContent.trim()).join(" ").trim();
          if (own && periodRe.test(own)) hits.push(`${tc(el)}="${own}"`);
          if (hits.length >= 30) break;
        }
        log("debug_dropdown period-value elements:", hits.join("  |  ") || "(none)");
        const sels = [...document.querySelectorAll("select")].filter((s) => s.offsetParent !== null);
        if (sels.length) {
          log("debug_dropdown native <select> found:", sels.map((s) =>
            `${tc(s)} opts=[${[...s.options].map((o) => o.text.trim()).join(",")}]`).join(" || "));
        }
      } catch (e) {
        log("debug_dropdown failed (non-fatal):", e.message);
      }
    } else if (step.action === "debug_rows") {
      // Diagnostic-only: dump the current page's row/cell structure right now
      // (e.g. while the default Detailed-Statement rows are showing, before a
      // date filter possibly empties the table). Pure logging, never fatal.
      try {
        await sleep(step.sleep_before || 0);
        debugRowSelectors(step.selector || "tr");
      } catch (e) {
        log("debug_rows failed (non-fatal):", e.message);
      }
    } else if (step.action === "wait_and_click_icase") {
      // Case-insensitive, tag-flexible click for SPA tabs/dropdowns (e.g. ICICI
      // Angular controls that aren't <a>/<button>). Mirrors the Playwright
      // click_icase: search across configurable tags, match visible text
      // (substring unless `exact`), skip oversized wrapper elements via
      // `max_text_len`, and click the SMALLEST matching element (the real
      // control, not its parent). Does NOT return — SPA clicks don't change the
      // URL, so we continue to the next step after `sleep_after`.
      try {
        const want = (step.text || "").toLowerCase();
        const tags = (step.tags && step.tags.length)
          ? step.tags
          : ["button", "a", "div", "span", "li", "[role='tab']", "[role='option']"];
        const exact = !!step.exact;
        const maxLen = step.max_text_len || 0;
        const finder = () => {
          const seen = new Set();
          let best = null, bestArea = Infinity;
          for (const sel of tags) {
            let nodes;
            try { nodes = document.querySelectorAll(sel); } catch (_) { continue; }
            for (const el of nodes) {
              if (seen.has(el)) continue;
              seen.add(el);
              if (el.offsetParent === null) continue;        // skip hidden (mobile/desktop dupes)
              const txt = (el.innerText || el.textContent || "").trim();
              if (!txt) continue;
              if (maxLen && txt.length > maxLen) continue;    // skip big wrappers
              const lc = txt.toLowerCase();
              const hit = exact ? (lc === want) : lc.includes(want);
              if (!hit) continue;
              const area = (el.offsetWidth || 0) * (el.offsetHeight || 0);
              if (area < bestArea) { bestArea = area; best = el; } // smallest = innermost control
            }
          }
          return best;
        };
        log(`Navigation (icase): looking for "${step.text}" in [${tags.join(", ")}]`);
        const el = await waitForElement(finder, step.timeout || 15000);
        log("Navigation (icase): clicking", (el.innerText || "").trim().slice(0, 40));
        el.scrollIntoView({ block: "center" });
        _robustClick(el);
        await sleep(step.sleep_after || 2000);
      } catch (e) {
        // Non-fatal: a missed intermediate step shouldn't kill the whole flow.
        // The logs show which step missed; content.js still tries the rest and
        // then waits for ready_selector.
        log("Navigation step (icase) failed (non-fatal):", e.message);
        // Diagnostic: dump the visible short-text candidates that ARE on screen
        // right now, so we can see what the dropdown/tab actually offers (e.g.
        // whether "Last 1 Year" is present, and under which tag/text).
        try {
          const want = (step.text || "").toLowerCase();
          const cand = [];
          for (const el of document.querySelectorAll("li,div,span,button,a,option,label")) {
            if (el.offsetParent === null) continue;
            if (el.children.length > 1) continue;          // leaf-ish only
            const t = (el.innerText || el.textContent || "").trim();
            if (!t || t.length > 24) continue;
            cand.push(`${el.tagName.toLowerCase()}:"${t}"`);
            if (cand.length >= 40) break;
          }
          log(`Nav-miss candidates (looking for "${step.text}"):`, cand.join("  |  "));
        } catch (_) {}
        await sleep(500);
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

  // ── 5 + 6. Paginate & scrape ──────────────────────────────────────────
  // Two pagination styles:
  //  • "replace" (ICICI): each page REPLACES the visible rows, so we must
  //    scrape every page as we go and accumulate, otherwise we'd only capture
  //    the last page. The "next" control is an icon after the "Page X of Y"
  //    text; we stop when X >= Y.
  //  • default (BoA etc.): pages ACCUMULATE (view-more / infinite scroll), so
  //    load everything first, then scrape once.
  const startCount = document.querySelectorAll(config.rows.selector).length;
  log(`Rows visible before pagination: ${startCount}`);
  showBanner(`Loading all transactions… (${startCount} so far)`);

  // THIN HARVEST: grab RAW innerText per configured cell and ship it. ALL
  // parsing/normalization — date parse + year-fix, credit/debit direction,
  // amount parse, empty-row skip, firstLine description, currency detect — now
  // runs server-side in scrape_normalize.py, so logic changes need NO extension
  // republish. The extension only does what must run in the bank page.
  let rawRows;
  if (config.pagination && config.pagination.mode === "replace") {
    rawRows = await harvestAllPagesReplace(config);
  } else {
    await loadAllPages(config);
    const afterCount = document.querySelectorAll(config.rows.selector).length;
    log(`Rows visible after pagination: ${afterCount}`);
    rawRows = harvestRawRows(config);
  }
  // Ending balance is shipped RAW (currency symbol intact) — the server derives
  // the numeric balance and detects the currency from it.
  const endingBalance = scrapeBalance(config);

  log(`Harvested ${rawRows.length} raw rows, balance: ${endingBalance}`);
  showBanner(`✅ Found ${rawRows.length} transactions — saving to FIFO…`, "success");
  setTimeout(removeBanner, 3000);

  chrome.runtime.sendMessage({
    type: "SCRAPE_RESULT",
    payload: { raw_rows: rawRows, endingBalance, currentUrl: window.location.href, navLog: _navLog() },
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

// Parse "Page X of Y" → {cur, total, el}. Returns the SMALLEST (innermost)
// element carrying that text — important because the whole pager box's innerText
// is ALSO "Page X of Y" (its arrow icons have no text), and matching the box
// would put its right edge past the last arrow, breaking the geometry that finds
// the "next" control beside the number.
function currentPageInfo(pattern) {
  if (!pattern) return null;
  const re = new RegExp(pattern, "i");
  let best = null, bestArea = Infinity;
  for (const el of document.querySelectorAll("div, span, p, li, b, strong, em")) {
    const t = (el.innerText || "").trim();
    if (t.length > 24 || !re.test(t)) continue;
    const m = t.match(/(\d+)\s*of\s*(\d+)/i);
    if (!m) continue;
    const r = el.getBoundingClientRect();
    const area = (r.width * r.height) || Infinity;
    if (area < bestArea) {
      bestArea = area;
      best = { cur: parseInt(m[1], 10), total: parseInt(m[2], 10), el };
    }
  }
  return best;
}

// A cheap signature of the current page's content, to detect when a "next"
// click has actually swapped the rows (page-info may not always update first).
function firstRowSignature(config) {
  const rows = document.querySelectorAll(config.rows.selector);
  if (!rows.length) return "";
  const f = rows[0], l = rows[rows.length - 1];
  return `${rows.length}|${(f.innerText || "").trim().slice(0, 60)}|${(l.innerText || "").trim().slice(0, 60)}`;
}

// Wait until the page content changes after a "next" click: either the page-info
// current number advances, or the row signature changes.
function waitForPageChange(config, prevCur, prevSig, timeoutMs) {
  const pattern = config.pagination && config.pagination.page_info_pattern;
  return new Promise((resolve) => {
    const start = Date.now();
    const t = setInterval(() => {
      const info = currentPageInfo(pattern);
      const sig = firstRowSignature(config);
      if ((info && prevCur != null && info.cur !== prevCur) || (sig && sig !== prevSig)) {
        clearInterval(t); resolve(true);
      } else if (Date.now() - start > timeoutMs) {
        clearInterval(t); resolve(false);
      }
    }, 250);
  });
}

// Replace-style pagination (ICICI): each page REPLACES the rows, so harvest
// every page and accumulate. Dedupe by the raw row's JSON in case pages overlap;
// the server dedupes again on store, so this is only an optimization.
async function harvestAllPagesReplace(config) {
  const all = [];
  const seen = new Set();
  const pattern = config.pagination.page_info_pattern;
  const MAX = config.pagination.max_pages || 60;

  for (let p = 0; p < MAX; p++) {
    const pageRows = harvestRawRows(config);
    let added = 0;
    for (const raw of pageRows) {
      const key = JSON.stringify(raw);
      if (seen.has(key)) continue;
      seen.add(key); all.push(raw); added++;
    }
    const info = currentPageInfo(pattern);
    log(`Page ${info ? info.cur + "/" + info.total : "?"}: harvested ${pageRows.length} rows (+${added} new, total ${all.length})`);
    showBanner(`Loading all transactions… (page ${info ? info.cur + " of " + info.total : p + 1}, ${all.length} so far)`);

    if (info && info.cur >= info.total) { log("Pagination: reached last page."); break; }

    const next = findNextPageButton(config.pagination.button_texts, pattern);
    if (!next) { log("Pagination: no next control found — stopping."); break; }

    const prevCur = info ? info.cur : null;
    const prevSig = firstRowSignature(config);
    next.scrollIntoView({ block: "center" });
    _robustClick(next);
    const changed = await waitForPageChange(config, prevCur, prevSig, 15000);
    if (!changed) { log("Pagination: page did not change after clicking next — stopping."); break; }
    await sleep(350);
  }
  log(`Pagination (replace): done — ${all.length} unique raw rows across pages.`);
  return all;
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

// ── Harvest RAW cell text per row (NO parsing — the server normalizes) ─────
// The extension is a dumb harvester: it grabs the raw innerText for each cell
// the bank's scrape-config maps, plus the raw date text and the row's pending
// flag (a DOM concern that must stay in the page), and ships it. The server's
// scrape_normalize.py does all parsing (amount, date + year-fix, credit/debit
// direction, empty-row skip, firstLine description, currency) — so logic
// iterates server-side with no extension republish. The raw-row keys match the
// config's cell-selector keys exactly; the server keys off the same names.
function harvestRawRows(config) {
  const rows = document.querySelectorAll(config.rows.selector);
  const r = config.rows;
  const CELL_KEYS = ["description", "credit", "debit", "balance", "amount", "amount_type"];
  const out = [];

  // One-time DOM diagnostic (set rows.debug_cells:true in scrape-config) — logs
  // the first matched row's <td> structure so selectors can be tuned against the
  // real (often obfuscated) bank DOM via the persisted fifoLog.
  if (r.debug_cells && rows.length && !globalThis.__fifoDebugCellsLogged) {
    globalThis.__fifoDebugCellsLogged = true;
    try {
      const sample = rows[0];
      const cells = [...sample.querySelectorAll("td")].map((td, i) =>
        `td[${i}].${(td.className || "").trim().replace(/\s+/g, ".") || "(noclass)"}=` +
        `"${(td.innerText || "").trim().slice(0, 30)}"`);
      log("ROW DEBUG: selector matched", rows.length, "rows. Row[0] cells:", cells.join("  |  "));
    } catch (e) { log("ROW DEBUG failed:", e.message); }
  }

  rows.forEach((row) => {
    try {
      const raw = {};
      for (const k of CELL_KEYS) {
        if (r[k]) raw[k] = text(row.querySelector(r[k]));
      }
      raw.date = harvestRawDate(config, row);          // raw text, server parses
      raw.pending = row.classList.contains("pending"); // DOM concern stays here
      out.push(raw);
    } catch (_) {}
  });
  return out;
}

// Raw date TEXT for a row (no parsing). Banks with a date column return its
// innerText; ICICI keeps the date in an ancestor block ("29 Jun", no year) — we
// walk up the DOM to find it and return the raw text. The server (parse_day_-
// month_date + fix_years_sequential) does the parsing and year inference.
function harvestRawDate(config, row) {
  const r = config.rows;
  if (r.date) return text(row.querySelector(r.date));
  if (r.date_mode === "parent_walk") {
    try {
      let el = row.parentElement;
      for (let i = 0; i < 10 && el; i++) {
        const dateEl = el.querySelector("div.date, div[class*='date']");
        if (dateEl) {
          const day = dateEl.querySelector("p.date-1, span.date-1")?.innerText?.trim();
          const month = dateEl.querySelector("p.date-2, span.date-2")?.innerText?.trim();
          if (day && month) return `${day} ${month}`;
          const full = dateEl.innerText?.trim();
          if (full) return full;
        }
        const prev = el.previousElementSibling;
        if (prev && /\d{1,2}[\s/-]\w+/i.test(prev.innerText || "")) return prev.innerText.trim();
        el = el.parentElement;
      }
    } catch (_) {}
  }
  return "";
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
  // 1. Plain text glyph match (">", "›", …) on real buttons/links.
  for (const el of document.querySelectorAll("button, a, span")) {
    const t = el.innerText?.trim();
    if ((buttonTexts || []).includes(t) && !el.disabled && el.getAttribute("aria-disabled") !== "true") return el;
  }
  // 2. aria-label / title containing "next".
  for (const el of document.querySelectorAll("button, a, [role='button'], svg, i, span, div")) {
    const lbl = ((el.getAttribute && (el.getAttribute("aria-label") || el.getAttribute("title"))) || "").toLowerCase();
    if (/\bnext\b/.test(lbl) && !el.disabled && el.getAttribute("aria-disabled") !== "true") return el;
  }
  // 3. Position-relative: the first clickable control AFTER the "Page X of Y"
  //    text. ICICI renders pager arrows as bare <svg> icons (no text/label), in
  //    order [first][prev][PageInfo][next][last]. If we're already on the last
  //    page (X >= Y), return null so pagination stops.
  if (pageInfoPattern) {
    const info = currentPageInfo(pageInfoPattern);
    if (info) {
      if (info.cur >= info.total) return null;       // on last page — done
      // Collect clickable pager controls near the "Page X of Y" text. Climb
      // parents (the text is itself a <div>, so closest('div') is useless) until
      // some controls turn up.
      let node = info.el.parentElement, controls = [];
      for (let up = 0; up < 6 && node && !controls.length; up++, node = node.parentElement) {
        controls = [...node.querySelectorAll("button, a, svg, i, [role='button']")]
          .filter((c) => c.offsetParent !== null && !c.contains(info.el) && !info.el.contains(c));
      }
      if (controls.length) {
        // DOM order is misleading: ICICI lists ALL arrow svgs after the page
        // number even though first/prev sit visually to its left. Use GEOMETRY —
        // "next" is the control immediately to the RIGHT of the page number on
        // the same row. (first/prev are left; "last" and any download button are
        // further right, so the CLOSEST right-side control is "next".)
        const r0 = info.el.getBoundingClientRect();
        if (r0.width > 0) {
          const cy = (r0.top + r0.bottom) / 2;
          const right = controls
            .map((c) => ({ c, r: c.getBoundingClientRect() }))
            .filter(({ r }) => r.width > 0 && r.left >= r0.right - 4 &&
                    Math.abs((r.top + r.bottom) / 2 - cy) < 60)
            .sort((a, b) => a.r.left - b.r.left);
          if (right.length) return right[0].c;
        }
        // Geometry unavailable (zero-size layout) — fall back to DOM order.
        const after = controls.filter((c) =>
          info.el.compareDocumentPosition(c) & Node.DOCUMENT_POSITION_FOLLOWING);
        if (after.length) return after[0];
      }
    }
  }
  return null;
}

// ── Helpers ───────────────────────────────────────────────────────────────
function text(el) { return el?.innerText?.trim() || ""; }

// normalizeDate stays in the extension ONLY for the pagination cutoff decision
// (oldestVisibleRowDate) — i.e. when to stop clicking "view more". It does NOT
// touch the harvested data: the server parses every transaction date. Amount,
// year-fix, and direction parsing have all moved server-side.
function normalizeDate(raw) {
  // BoA (and others) label a not-yet-posted row "Pending" or "Processing" in
  // the date column. Both must map to the pending sentinel — otherwise the
  // `new Date(`${raw} ${year}`)` fallback below silently coerces "Processing"
  // to 2026-01-01 (V8 grabs the year, defaults to Jan 1), which then drops out
  // of the dashboard's recent-window date filter and the row vanishes.
  if (!raw || /^(pending|processing)\b/i.test(raw.trim())) return "Pending";
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

    // If the primary row selector actually matches rows, dump the FIRST row's
    // full cell structure (tag.class="text"). This is the authoritative source
    // for the description/credit/debit/balance selectors — far more reliable
    // than the currency-anchored walk below, which can latch onto the account
    // summary card instead of a transaction row.
    if (parts.length) {
      const matched = document.querySelectorAll(parts[0]);
      if (matched.length) {
        const tc = (el) =>
          `${el.tagName.toLowerCase()}.${(el.className || "").toString().trim().replace(/\s+/g, ".") || "(noclass)"}`;
        const row0 = matched[0];
        // The <td> column classes are what credit_debit_cols needs (credit vs
        // debit vs balance column). Dump each direct <td> with its class + text.
        const tds = [...row0.querySelectorAll("td")]
          .map((td, i) => `td[${i}].${(td.className || "").toString().trim().replace(/\s+/g, ".") || "(noclass)"}="${(td.innerText || "").trim().replace(/\s+/g, " ").slice(0, 30)}"`);
        log(`PRIMARY ROW[0] TDs:`, tds.join("  |  "));
        const cells = [...row0.querySelectorAll("td, th, p, span, div")]
          .filter((el) => el.children.length === 0 && (el.innerText || "").trim())
          .slice(0, 18)
          .map((el) => `${tc(el)}="${(el.innerText || "").trim().slice(0, 26)}"`);
        log(`PRIMARY ROW[0] leaf cells:`, cells.join("  |  "));
      }
    }

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

    // Currency-anchored probe: modern ICICI renders rows as <div>s, not <tr>s,
    // so the tr-based sample above comes back empty. Find elements whose text is
    // a money value (₹1,234.56), then walk up to the repeating ROW container
    // (the nearest ancestor that has several same-class siblings) and dump its
    // tag.class plus the tag.class of each descendant cell. That's exactly the
    // info needed to write the selector/description/credit/debit/balance config.
    const money = /(₹|rs\.?|inr)\s*[\d,]+\.\d{2}/i;
    const moneyEls = [...document.querySelectorAll("div,span,p,td")]
      .filter((el) => el.children.length === 0 && money.test((el.innerText || "").trim()));
    log("Currency-text elements found:", moneyEls.length);
    if (moneyEls.length) {
      let row = moneyEls[0];
      // climb until the element has >=3 siblings sharing its className (a list)
      for (let hops = 0; hops < 8 && row.parentElement; hops++) {
        const p = row.parentElement;
        const sibs = [...p.children].filter(
          (c) => c.className && c.className === row.className,
        );
        if (sibs.length >= 3) break;
        row = p;
      }
      const tc = (el) =>
        `${el.tagName.toLowerCase()}.${(el.className || "").toString().trim().replace(/\s+/g, ".") || "(noclass)"}`;
      log("ROW CONTAINER candidate:", tc(row), "siblings=",
        [...(row.parentElement ? row.parentElement.children : [])]
          .filter((c) => c.className === row.className).length);
      const cells = [...row.querySelectorAll("*")]
        .filter((el) => el.children.length === 0 && (el.innerText || "").trim())
        .slice(0, 14)
        .map((el) => `${tc(el)}="${(el.innerText || "").trim().slice(0, 24)}"`);
      log("ROW CONTAINER cells:", cells.join("  |  "));
    }
  } catch (e) {
    log("Selector probe failed:", e.message);
  }
}

function sendError(msg) {
  log("ERROR:", msg);
  chrome.runtime.sendMessage({ type: "SCRAPE_RESULT", payload: { error: msg, navLog: _navLog() } });
}

// Fire ONE realistic click gesture. Custom Angular controls (e.g. ICICI's period
// dropdown) often open on pointerdown/mousedown rather than a bare synthetic
// `click`, so dispatch the full pointer→mouse→click chain. CRUCIAL: this counts
// as a SINGLE activation. Do NOT also call el.click() — that's a second click,
// and on a toggle (an open dropdown) it immediately closes it again, so the
// option we want disappears before we can pick it. One gesture = one toggle.
function _robustClick(el) {
  try { el.focus(); } catch (_) {}
  const opts = { bubbles: true, cancelable: true, view: window };
  for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click"]) {
    try {
      const Ev = type.startsWith("pointer") && typeof PointerEvent === "function" ? PointerEvent : MouseEvent;
      el.dispatchEvent(new Ev(type, opts));
    } catch (_) {}
  }
}

// Persist content.js logs into the SAME chrome.storage.local "fifoLog" ring
// buffer that background.js uses, so the navigate-step and ROW DEBUG output is
// visible in the dump even after the (often background) tab closes. Content
// scripts can use chrome.storage when the extension has the "storage" perm.
// NOTE: background.js re-injects content.js into the SAME tab on every Sync Now,
// so this file's top-level scope can run twice in one page. A top-level
// `let`/`const` would throw "already declared" on the 2nd injection. Keep the
// log-chain state on globalThis (idempotent) and avoid any top-level let/const.
function _persistLog(line) {
  globalThis.__fifoLogChain = (globalThis.__fifoLogChain || Promise.resolve()).then(async () => {
    try {
      const { fifoLog = [] } = await chrome.storage.local.get("fifoLog");
      fifoLog.push(line);
      if (fifoLog.length > 500) fifoLog.splice(0, fifoLog.length - 500);
      await chrome.storage.local.set({ fifoLog });
    } catch (_) {}
  });
}
function log(...args) {
  console.log("[FIFO]", ...args);
  try {
    const line = `[${new Date().toISOString()}] [content] ` +
      args.map(a => (typeof a === "string" ? a : JSON.stringify(a))).join(" ");
    _persistLog(line);
    // Also keep an in-memory copy so it can ride along in the SCRAPE_RESULT
    // payload — background.js persists that, which survives the tab being closed
    // on failure (the async chrome.storage writes above often don't flush first).
    (globalThis.__fifoNavLog || (globalThis.__fifoNavLog = [])).push(line);
  } catch (_) {}
}
function _navLog() {
  return (globalThis.__fifoNavLog || []).slice(-150);
}

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
