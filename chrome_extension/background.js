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
    chrome.storage.local.get(["fifoUrl", "fifoAccountByBank"], async ({ fifoUrl = DEFAULT_FIFO, fifoAccountByBank = {} }) => {
      try {
        const r = await fetch(`${fifoUrl}/api/extension/scrape-config?domain=${encodeURIComponent(msg.domain)}`);
        if (!r.ok) { sendResponse({ error: "not_supported" }); return; }
        const config = await r.json();
        // Augment config with per-account state so content.js can stop
        // pagination once it reaches dates we've already stored. smartConnectSync
        // populates fifoAccountByBank in chrome.storage.local before injecting.
        const acctMeta = fifoAccountByBank[config.bank_key] || null;
        if (acctMeta?.last_txn_date) {
          config.last_txn_date = acctMeta.last_txn_date;
        }
        sendResponse({ config });
      } catch (e) {
        sendResponse({ error: e.message });
      }
    });
    return true;
  }

  // Bridge script requests a sync for a specific bank (triggered from FIFO demo page)
  if (msg.type === "SMART_CONNECT_SYNC") {
    const { bank_key, apiBase, authToken } = msg;
    const fifoUrl = apiBase || DEFAULT_FIFO;
    const fifoTabId = _sender?.tab?.id ?? null;
    smartConnectSync(bank_key, fifoUrl, fifoTabId, sendResponse, authToken);
    return true;
  }
});

// Build fetch options that include the FIFO auth token if we have one.
// All backend endpoints became authenticated in the multi-user refactor;
// unauthenticated calls now return 401 with {error: "Malformed token"},
// which previously crashed `accounts.find(...)` because the response wasn't an array.
function _authHeaders(authToken) {
  return authToken ? { "Authorization": `Bearer ${authToken}` } : {};
}

// ── Smart Connect sync (triggered from FIFO demo page) ────────────────────
async function smartConnectSync(bank_key, fifoUrl, fifoTabId, sendResponse, authToken) {
  try {
    if (!authToken) {
      sendResponse({ ok: false, error: "Not signed in. Open FIFO and log in, then try again." });
      return;
    }
    // Cache the token so the 6-hour alarm-driven syncAllAccounts can reuse it
    // when the user isn't actively on the FIFO page.
    await chrome.storage.local.set({ fifoAuthToken: authToken, fifoUrl });
    const r = await fetch(`${fifoUrl}/api/accounts`, { headers: _authHeaders(authToken) });
    if (!r.ok) {
      sendResponse({ ok: false, error: `Auth check failed (${r.status}). Please sign out and back in.` });
      return;
    }
    const accounts = await r.json();
    if (!Array.isArray(accounts)) {
      sendResponse({ ok: false, error: "Unexpected /api/accounts response shape." });
      return;
    }
    const account = accounts.find(a => a.bank_key === bank_key);

    // Stash per-bank account meta so GET_SCRAPE_CONFIG can inject
    // last_txn_date into the config returned to content.js. content.js
    // uses it to stop paginating once it reaches dates we already have.
    const fifoAccountByBank = {};
    for (const a of accounts) {
      if (a.bank_key) {
        fifoAccountByBank[a.bank_key] = {
          id:             a.id,
          last_txn_date:  a.last_txn_date || null,
          last_synced_at: a.last_synced_at || null,
        };
      }
    }
    await chrome.storage.local.set({ fifoAccountByBank });

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
    const syncResult = await syncBank(acct, fifoUrl, startUrl, isFirstConnect ? 600_000 : 180_000, isFirstConnect, fifoTabId, authToken);

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

// Root domain used to find the user's already-open bank tab regardless of
// which subdomain they're currently on (e.g., secure.bankofamerica.com vs
// www.bankofamerica.com).
function _bankKeyToRootDomain(bank_key) {
  const map = {
    capital_one: "capitalone.com",
    boa:         "bankofamerica.com",
    icici:       "icici.bank.in",
    discover:    "discover.com",
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
  const { fifoUrl = DEFAULT_FIFO, fifoAuthToken } = await chrome.storage.local.get(["fifoUrl", "fifoAuthToken"]);

  if (!fifoAuthToken) {
    log("syncAllAccounts: no cached auth token — user needs to sign in to FIFO and trigger Sync Now once to seed the token.");
    return;
  }

  // Fetch accounts from FIFO backend (auth required since the user-isolation refactor)
  let accounts;
  try {
    const r = await fetch(`${fifoUrl}/api/accounts`, { headers: _authHeaders(fifoAuthToken) });
    if (r.status === 401) {
      log("syncAllAccounts: cached token rejected (probably expired). Clearing and giving up until next Sync Now.");
      await chrome.storage.local.remove("fifoAuthToken");
      return;
    }
    accounts = await r.json();
  } catch (e) {
    await setError(`Cannot reach FIFO backend at ${fifoUrl}: ${e.message}`);
    return;
  }
  if (!Array.isArray(accounts)) {
    log("syncAllAccounts: unexpected /api/accounts shape; skipping.");
    return;
  }

  // Sync each bank we know how to handle (alarm-driven syncs pass the cached
  // auth token through so backend calls succeed without an active user).
  for (const acct of accounts) {
    try {
      if (acct.bank_key === "capital_one") {
        await syncBank(acct, fifoUrl, "https://myaccounts.capitalone.com/accountSummary", 120_000, false, null, fifoAuthToken);
      } else if (acct.bank_key === "icici") {
        if (!acct.last_url) {
          log(`ICICI (account #${acct.id}): no last_url — please do a manual login first.`);
          continue;
        }
        await syncBank(acct, fifoUrl, acct.last_url, 180_000, false, null, fifoAuthToken);
      } else if (acct.bank_key === "boa") {
        const boaUrl = acct.last_url || _bankKeyToStartUrl("boa");
        if (!boaUrl) {
          log(`BOA (account #${acct.id}): no last_url — skipping background sync.`);
          continue;
        }
        await syncBank(acct, fifoUrl, boaUrl, 120_000, false, null, fifoAuthToken);
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
async function syncBank(account, fifoUrl, startUrl, timeoutMs, firstConnect = false, fifoTabId = null, authToken = null) {
  log(`Syncing ${account.bank_key} (account #${account.id}) — opening ${startUrl} [firstConnect=${firstConnect}]`);
  await chrome.storage.local.set({ syncing: true });

  let tab;
  let backgroundWindow = null;       // only set for first-connect (visible) or as last-resort hidden window
  let usingExistingTab = false;      // true when we reused a tab the user already had open
  let navListener;
  let keepAlive;
  let _syncResult = null;            // populated on successful ingest, returned to caller
  try {
    if (firstConnect) {
      // First connect: user must log in interactively. Visible focused tab.
      tab = await chrome.tabs.create({ url: startUrl, active: true });
    } else {
      // SYNC NOW (silent): try the user's already-open tab for this bank
      // before opening any window at all. Chrome doesn't allow truly
      // invisible new windows on macOS (windows.create always shows a
      // Dock flash), and opening one anyway gives a confusing UX. If the
      // user is already logged into BoA in some tab, we just inject our
      // scraper there and everything happens in-place — no flash, no
      // visible window, no new tab in their strip.
      // Match any subdomain of the bank's root domain. The active logged-in
      // page lives on secure.bankofamerica.com / retailnetbanking.icici.bank.in
      // etc — different from the _bankKeyToDomain() map. Use the root domain
      // so we catch whichever subdomain the user is currently on.
      const rootDomain = _bankKeyToRootDomain(account.bank_key);
      const existingTabs = rootDomain
        ? await chrome.tabs.query({ url: `*://*.${rootDomain}/*` })
        : [];
      if (existingTabs.length > 0) {
        tab = existingTabs[0];
        usingExistingTab = true;
        log(`Reusing existing ${account.bank_key} tab #${tab.id} (current url: ${tab.url})`);
        // CRITICAL: do NOT navigate the user's tab. We don't know if their
        // current page has fresh session tokens that a navigation would
        // invalidate. Their stored `last_url` is often a single-use redirect
        // URL that we'd ruin by visiting again. Just inject content.js into
        // whatever page they have open and let content.js decide:
        //   - if rows are visible → scrape
        //   - if not → return NOT_LOGGED_IN / NO_ROWS so the UI tells the
        //     user to navigate to their activity page and try again.
        log("Injecting content.js into the existing tab (no navigation)");
        try {
          await chrome.scripting.executeScript({
            target: { tabId: tab.id },
            files:  ["content.js"],
          });
        } catch (e) {
          log("Direct content.js inject failed:", e.message);
          throw new Error(
            `Couldn't run on the ${account.display_name || account.bank_key} tab. ` +
            `Make sure you're on the activity page and try again.`
          );
        }
      } else {
        // No existing tab — tell the caller we can't do a silent sync without
        // one. The UI will prompt the user to open the bank and try again.
        throw new Error(
          `Open ${account.display_name || account.bank_key} in a tab (and log in) before clicking Sync now. ` +
          `Sync now uses your existing logged-in session — it doesn't open a new login window.`
        );
      }
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

    const result = await waitForScrapeResult(tab.id, timeoutMs, { ignoreLoginErrors: firstConnect });

    // Sync Now is a strictly-silent feature: it uses your existing bank
    // session and never asks for re-login inline. If content.js reports
    // NOT_LOGGED_IN, we bail with a clear message. The user then explicitly
    // clicks Re-login (a separate action) which opens a visible tab.
    if (result.error === "NOT_LOGGED_IN") {
      // User didn't log in within the window, OR this was a first-connect
      // already (no further promotion possible). Mark the account so the
      // sidebar shows a re-login badge.
      if (account.id) {
        try {
          await fetch(`${fifoUrl}/api/accounts/${account.id}`, {
            method: "PATCH",
            headers: { "Content-Type": "application/json", ..._authHeaders(authToken) },
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
      headers: { "Content-Type": "application/json", ..._authHeaders(authToken) },
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
      // Hidden background sync (first-connect or no existing tab): tear
      // down the minimized window we opened.
      try { await chrome.windows.remove(backgroundWindow.id); } catch (_) {}
    } else if (usingExistingTab) {
      // We reused the user's existing bank tab — do NOT close it. Just
      // refocus the FIFO tab so the user sees the sync result there.
      if (fifoTabId != null) {
        try {
          await chrome.tabs.update(fifoTabId, { active: true });
        } catch (_) {}
      }
    } else if (tab) {
      // First-connect via a brand-new tab we opened: close it + refocus FIFO.
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
