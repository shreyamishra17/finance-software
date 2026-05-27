// FIFO Sync — Background Service Worker
// Runs every 6 hours, opens hidden tabs for each connected bank,
// scrapes transactions via content scripts, sends to FIFO backend.

const SYNC_ALARM   = "fifo-sync";
const SYNC_MINUTES = 360; // 6 hours
const DEFAULT_FIFO = "http://localhost:8000";

// ── Startup ───────────────────────────────────────────────────────────────
chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create(SYNC_ALARM, { periodInMinutes: SYNC_MINUTES });
  log("FIFO Sync installed. Alarm set for every 6 hours.");
});

// Re-register alarm on service worker restart (MV3 workers can be killed)
chrome.alarms.get(SYNC_ALARM, (alarm) => {
  if (!alarm) {
    chrome.alarms.create(SYNC_ALARM, { periodInMinutes: SYNC_MINUTES });
  }
});

// ── Alarm handler ─────────────────────────────────────────────────────────
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === SYNC_ALARM) {
    log("Alarm fired — starting sync…");
    syncAllAccounts();
  }
});

// ── Message from popup (manual sync trigger) ──────────────────────────────
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg.type === "SYNC_NOW") {
    log("Manual sync triggered from popup");
    syncAllAccounts().then(() => sendResponse({ ok: true })).catch(e => sendResponse({ ok: false, error: e.message }));
    return true;
  }
  if (msg.type === "GET_STATUS") {
    chrome.storage.local.get(["lastSync", "lastError", "syncing"], (data) => {
      sendResponse(data);
    });
    return true;
  }
  // Content script asks background to fetch scrape config (avoids localhost permission issue)
  if (msg.type === "GET_SCRAPE_CONFIG") {
    chrome.storage.local.get(["fifoUrl"], async ({ fifoUrl = DEFAULT_FIFO }) => {
      try {
        const r = await fetch(`${fifoUrl}/api/extension/scrape-config?domain=${encodeURIComponent(msg.domain)}`);
        if (!r.ok) { sendResponse({ error: "not_supported" }); return; }
        const config = await r.json();
        sendResponse({ config });
      } catch (e) {
        sendResponse({ error: e.message });
      }
    });
    return true;
  }

  // Bridge script requests a sync for a specific bank (triggered from FIFO demo page)
  if (msg.type === "SMART_CONNECT_SYNC") {
    const { bank_key, apiBase } = msg;
    const fifoUrl = apiBase || DEFAULT_FIFO;
    const fifoTabId = _sender?.tab?.id ?? null;
    smartConnectSync(bank_key, fifoUrl, fifoTabId, sendResponse);
    return true;
  }
});

// ── Smart Connect sync (triggered from FIFO demo page) ────────────────────
async function smartConnectSync(bank_key, fifoUrl, fifoTabId, sendResponse) {
  try {
    const r = await fetch(`${fifoUrl}/api/accounts`);
    const accounts = await r.json();
    const account = accounts.find(a => a.bank_key === bank_key);

    // Determine start URL — prefer last_url (exact transactions page from prior login)
    const startUrl = account?.last_url || _bankKeyToStartUrl(bank_key);
    if (!startUrl) {
      sendResponse({ ok: false, error: `No start URL for ${bank_key}. Please do a manual login first.` });
      return;
    }

    // First connect (no account yet) or no last_url → open tab visibly so the user
    // can log in. The tab stays open until content.js finds transactions (up to 10 min).
    const isFirstConnect = !account || !account.last_url;
    const acct = account || { id: null, bank_key, display_name: bank_key };
    const syncResult = await syncBank(acct, fifoUrl, startUrl, isFirstConnect ? 600_000 : 180_000, isFirstConnect, fifoTabId);

    // syncResult is null when syncBank's outer try/catch swallowed an error
    // (e.g. window creation failed). Surface that as a real error to the UI
    // instead of falsely reporting "Synced — N transactions" with a stale count.
    if (!syncResult) {
      const { lastError } = await chrome.storage.local.get("lastError");
      sendResponse({ ok: false, error: lastError || "Sync did not complete — see service worker console for details." });
      return;
    }
    sendResponse({
      ok:        true,
      count:     syncResult.newCount ?? 0,
      received:  syncResult.scrapedCount ?? 0,
    });
  } catch (e) {
    sendResponse({ ok: false, error: e.message });
  }
}

function _bankKeyToDomain(bank_key) {
  const map = {
    capital_one: "myaccounts.capitalone.com",
    boa:         "www.bankofamerica.com",
    icici:       "retailnetbanking.icici.bank.in",
    discover:    "card.discover.com",
  };
  return map[bank_key] || "";
}

function _bankKeyToStartUrl(bank_key) {
  const map = {
    capital_one: "https://myaccounts.capitalone.com/accountSummary",
    boa:         "https://secure.bankofamerica.com/auth/signon/",
    icici:       "https://retailnetbanking.icici.bank.in/",
  };
  return map[bank_key] || null;
}

// ── Main sync orchestrator ────────────────────────────────────────────────
async function syncAllAccounts() {
  const { fifoUrl = DEFAULT_FIFO } = await chrome.storage.local.get("fifoUrl");

  // Fetch accounts from FIFO backend
  let accounts;
  try {
    const r = await fetch(`${fifoUrl}/api/accounts`);
    accounts = await r.json();
  } catch (e) {
    await setError(`Cannot reach FIFO backend at ${fifoUrl}: ${e.message}`);
    return;
  }

  // Sync each bank we know how to handle
  for (const acct of accounts) {
    try {
      if (acct.bank_key === "capital_one") {
        await syncBank(acct, fifoUrl, "https://myaccounts.capitalone.com/accountSummary", 120_000);
      } else if (acct.bank_key === "icici") {
        if (!acct.last_url) {
          log(`ICICI (account #${acct.id}): no last_url — please do a manual login first.`);
          continue;
        }
        await syncBank(acct, fifoUrl, acct.last_url, 180_000);
      } else if (acct.bank_key === "boa") {
        const boaUrl = acct.last_url || _bankKeyToStartUrl("boa");
        if (!boaUrl) {
          log(`BOA (account #${acct.id}): no last_url — skipping background sync.`);
          continue;
        }
        await syncBank(acct, fifoUrl, boaUrl, 120_000);
      } else {
        log(`Skipping unsupported bank: ${acct.bank_key}`);
      }
    } catch (e) {
      log(`Error syncing ${acct.bank_key} (account #${acct.id}): ${e.message}`);
    }
  }
}

// ── Generic bank sync (opens a tab, waits for SCRAPE_RESULT) ─────────────
// firstConnect=true → tab is visible so the user can log in manually;
//                     we ignore "not logged in" signals and keep waiting.
// firstConnect=false → tab is hidden (background sync); fail fast on expiry.
async function syncBank(account, fifoUrl, startUrl, timeoutMs, firstConnect = false, fifoTabId = null) {
  log(`Syncing ${account.bank_key} (account #${account.id}) — opening ${startUrl} [firstConnect=${firstConnect}]`);
  await chrome.storage.local.set({ syncing: true });

  let tab;
  let backgroundWindow = null; // only set for hidden background syncs
  let navListener;
  let keepAlive;
  let _syncResult = null;      // populated on successful ingest, returned to caller
  try {
    if (firstConnect) {
      // First connect = user must log in. Tab needs to be visible & focused.
      tab = await chrome.tabs.create({ url: startUrl, active: true });
    } else {
      // Background sync = silent. Open in a separate minimized window so it
      // never appears in the user's tab strip or steals focus. The window is
      // removed entirely in the finally block.
      // Chrome rejects `state` combined with `left`/`top`/`width`/`height` in
      // windows.create — they're mutually exclusive. So we create at a tiny
      // off-screen size first, then update to minimized in a second call.
      backgroundWindow = await chrome.windows.create({
        url:     startUrl,
        focused: false,
        type:    "normal",
        left:    -10000,   // off-screen so any brief flash is invisible
        top:     -10000,
        width:   400,
        height:  300,
      });
      // Minimize immediately on a separate call (this DOES accept state).
      try { await chrome.windows.update(backgroundWindow.id, { state: "minimized", focused: false }); } catch (_) {}
      tab = backgroundWindow.tabs?.[0];
      if (!tab) throw new Error("Failed to create background sync window");
    }

    // MV3 service workers are killed after ~30s of inactivity.
    // Poll storage every 20s so the worker stays alive for the full sync window.
    keepAlive = setInterval(() => chrome.storage.local.get("syncing"), 20000);

    // Safety-net: re-inject content.js on every navigation, including SPA pushState.
    // - Full page load: changeInfo.status === "complete"
    // - SPA pushState: changeInfo.url set but no status change
    // content.js has a per-URL guard to prevent double-execution on the same page.
    navListener = (tabId, changeInfo) => {
      if (tabId !== tab.id) return;
      if (changeInfo.status === "complete") {
        chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] })
          .catch(() => {});
      } else if (changeInfo.url) {
        // SPA navigation: URL changed without a full page load — wait for DOM to settle
        setTimeout(() => {
          chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] })
            .catch(() => {});
        }, 600);
      }
    };
    chrome.tabs.onUpdated.addListener(navListener);

    let result = await waitForScrapeResult(tab.id, timeoutMs, { ignoreLoginErrors: firstConnect });

    // ── Auto-promote on session expired ─────────────────────────────────
    // We started this sync in a minimized hidden window expecting valid
    // cookies. If content.js reports NOT_LOGGED_IN, instead of giving up,
    // promote the window: un-minimize, focus, resize so the user can finish
    // the login. The navListener stays attached, so once the user logs in
    // and the bank navigates back to a page with transaction rows, a fresh
    // content.js instance fires and the scrape continues silently.
    if (result.error === "NOT_LOGGED_IN" && backgroundWindow && !firstConnect) {
      log(`${account.bank_key}: session expired in hidden window — promoting to visible for re-login`);
      try {
        await chrome.windows.update(backgroundWindow.id, {
          state:   "normal",
          focused: true,
          width:   1100,
          height:  800,
        });
      } catch (e) {
        log("Promote window failed:", e.message);
      }
      // Give the user up to 10 minutes to complete login. waitForScrapeResult
      // will resolve when content.js (re-injected by navListener after the
      // bank navigates) successfully scrapes rows.
      result = await waitForScrapeResult(tab.id, 600_000, { ignoreLoginErrors: true });
    }

    if (result.error === "NOT_LOGGED_IN") {
      // User didn't log in within the window, OR this was a first-connect
      // already (no further promotion possible). Mark the account so the
      // sidebar shows a re-login badge.
      if (account.id) {
        try {
          await fetch(`${fifoUrl}/api/accounts/${account.id}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ needs_login: true, status: "needs_relogin" }),
          });
        } catch (_) {}
      }
      await setError(`${account.display_name || account.bank_key} session expired — please re-login.`);
      return;
    }
    if (result.error) {
      await setError(`${account.display_name || account.bank_key} sync failed: ${result.error}`);
      return;
    }

    log(`Scraped ${result.transactions.length} transactions from ${account.bank_key}, sending to FIFO…`);

    const resp = await fetch(`${fifoUrl}/api/smart-connect/extension-ingest`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        bank_key:       account.bank_key,
        transactions:   result.transactions,
        ending_balance: result.endingBalance || "",
        last_url:       result.currentUrl || "",
      }),
    });

    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "FIFO returned an error");

    // Stash the result on the outer try-scoped variable so the finally block
    // can return it to the caller (smartConnectSync uses it to report the
    // accurate count to the UI).
    _syncResult = {
      newCount:     data.count ?? 0,           // newly-inserted rows
      scrapedCount: result.transactions.length, // total rows scraped
    };

    log(`${account.bank_key} sync complete — ${_syncResult.newCount} new / ${_syncResult.scrapedCount} scraped.`);
    await chrome.storage.local.set({
      lastSync:  new Date().toISOString(),
      lastError: null,
      syncing:   false,
    });

  } catch (e) {
    await setError(e.message);
  } finally {
    clearInterval(keepAlive);
    if (navListener) chrome.tabs.onUpdated.removeListener(navListener);
    if (backgroundWindow) {
      // Hidden background sync: tear down the whole minimized window.
      try { await chrome.windows.remove(backgroundWindow.id); } catch (_) {}
    } else if (tab) {
      // First-connect: just close the bank tab and refocus the FIFO tab.
      try { await chrome.tabs.remove(tab.id); } catch (_) {}
      if (fifoTabId != null) {
        try {
          await chrome.tabs.update(fifoTabId, { active: true });
          const fifoTab = await chrome.tabs.get(fifoTabId);
          if (fifoTab?.windowId != null) {
            await chrome.windows.update(fifoTab.windowId, { focused: true });
          }
        } catch (_) { /* tab may have been closed by the user */ }
      }
    }
    await chrome.storage.local.set({ syncing: false });
  }
  return _syncResult;  // { newCount, scrapedCount } on success, null on failure
}

// ── Wait for content script to scrape and message back ───────────────────
// ignoreLoginErrors=true: skip NOT_LOGGED_IN and "no rows" results — used
// during first-connect when the user is still navigating to transactions.
function waitForScrapeResult(tabId, timeoutMs, { ignoreLoginErrors = false } = {}) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.runtime.onMessage.removeListener(handler);
      reject(new Error(`Timed out waiting for scrape result after ${timeoutMs / 1000}s`));
    }, timeoutMs);

    function handler(msg, sender) {
      if (msg.type !== "SCRAPE_RESULT" || sender.tab?.id !== tabId) return;
      const p = msg.payload;
      // During first connect the user is still logging in / navigating — skip
      // transient failures and keep waiting until transactions are actually found.
      if (ignoreLoginErrors && (
        p.error === "NOT_LOGGED_IN" ||
        (p.error && p.error.includes("No transaction rows"))
      )) {
        log("Ignoring transient error during first connect:", p.error);
        return;
      }
      clearTimeout(timer);
      chrome.runtime.onMessage.removeListener(handler);
      resolve(p);
    }
    chrome.runtime.onMessage.addListener(handler);
  });
}

// ── Helpers ───────────────────────────────────────────────────────────────
async function setError(msg) {
  log("ERROR:", msg);
  await chrome.storage.local.set({ lastError: msg, syncing: false });
}

function log(...args) {
  console.log("[FIFO]", ...args);
}
