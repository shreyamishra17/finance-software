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

// Catch-up sync on Chrome launch. The 6-hour alarm only ticks while Chrome is
// running, so a tick scheduled while the Mac was asleep/off is missed. When the
// user reopens Chrome we run one sync immediately (if the last sync is older
// than the normal interval) instead of waiting up to 6 more hours for the next
// tick. This is the honest answer to the "closed laptop" gap — device trust is
// machine-bound, so sync can only happen while this real Chrome is awake.
chrome.runtime.onStartup.addListener(async () => {
  try {
    const { lastSync } = await chrome.storage.local.get("lastSync");
    const stale = !lastSync ||
      (Date.now() - new Date(lastSync).getTime()) > SYNC_MINUTES * 60 * 1000;
    if (stale) {
      log("Chrome started — last sync is stale, running catch-up sync…");
      syncAllAccounts();
    } else {
      log("Chrome started — last sync still fresh, skipping catch-up.");
    }
  } catch (e) {
    log("onStartup catch-up check failed:", e.message);
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
    discover:    "https://card.discover.com/cardmembersvcs/achome/homepage",
  };
  return map[bank_key] || null;
}

// ── Headless fallback (unified sync ladder, step 2) ───────────────────────
// When the live-session path can't run (session expired, or no open bank
// tab) AND the user saved their password, ask the FIFO backend to log in
// headlessly via Playwright (POST /api/accounts/<id>/sync, which reads the
// OS Keychain). Returns a STATUS object so callers can react precisely:
//   { status: "ok",      newCount, scrapedCount }  — synced, badge cleared
//   { status: "relogin", reason, error }           — GENUINE OTP / expired
//                                                     session: a human must
//                                                     re-login (HTTP 422)
//   { status: "error",   error }                    — TECHNICAL failure (HTTP
//                                                     500 / network). NOT an
//                                                     OTP case — callers must
//                                                     NOT raise a re-login
//                                                     prompt (anti-false-alarm)
//   { status: "skip" }                              — no saved password to try
async function _headlessSyncFallback(account, fifoUrl, authToken) {
  if (!account?.id) return { status: "skip" };
  // Only attempt if a password is actually saved — otherwise there's nothing
  // to log in with.
  if (!account.has_credentials) {
    log(`${account.bank_key} (#${account.id}): no saved password — skipping headless fallback.`);
    return { status: "skip" };
  }
  log(`${account.bank_key} (#${account.id}): live session unavailable — trying headless login with saved password…`);
  try {
    const resp = await fetch(`${fifoUrl}/api/accounts/${account.id}/sync`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ..._authHeaders(authToken) },
      body: JSON.stringify({}),
    });
    const data = await resp.json().catch(() => ({}));
    if (resp.ok) {
      log(`${account.bank_key} (#${account.id}): headless sync succeeded — ${data.count ?? 0} new transactions.`);
      // Clear any stale needs_login badge now that we synced successfully.
      try {
        await fetch(`${fifoUrl}/api/accounts/${account.id}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json", ..._authHeaders(authToken) },
          body: JSON.stringify({ needs_login: false, status: "connected" }),
        });
      } catch (_) {}
      return { status: "ok", newCount: data.count ?? 0, scrapedCount: data.count ?? 0 };
    }
    if (resp.status === 422) {
      // Server already separated genuine "human re-login needed" (OTP /
      // expired session / no session) from technical errors. 422 == genuine.
      log(`${account.bank_key} (#${account.id}): headless sync needs human re-login [${data.reason || "?"}]: ${data.error || ""}`);
      return { status: "relogin", reason: data.reason, error: data.error };
    }
    if (resp.status === 409 || data.reason === "cancelled") {
      // Sync was deliberately cancelled (user closed the FIFO window). Benign —
      // NOT an error, NOT a re-login. Just skip; next cycle will retry.
      log(`${account.bank_key} (#${account.id}): headless sync cancelled (window closed).`);
      return { status: "skip" };
    }
    // 500 / other == technical failure. Do NOT treat as OTP/re-login.
    log(`${account.bank_key} (#${account.id}): headless sync technical error (${resp.status}): ${data.error || "unknown"}`);
    return { status: "error", error: data.error };
  } catch (e) {
    log(`${account.bank_key} (#${account.id}): headless sync network error: ${e.message}`);
    return { status: "error", error: e.message };
  }
}

// Flag an account as genuinely needing a human re-login (OTP / expired session).
// IMPORTANT: only call this for GENUINE re-login situations — never for plain
// technical failures, which must retry silently next cycle instead of nagging
// the user with a false "re-login required" alarm.
async function _markNeedsLogin(account, fifoUrl, authToken) {
  if (!account?.id) return;
  try {
    await fetch(`${fifoUrl}/api/accounts/${account.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", ..._authHeaders(authToken) },
      body: JSON.stringify({ needs_login: true, status: "needs_relogin" }),
    });
  } catch (_) {}
}

// ── Background-tab auto-login (desktop, real Chrome) ──────────────────────
// Fetch the saved login config (creds + form selectors) for an account.
// Returns null if there are no saved credentials or the call fails.
async function _fetchAutoLoginConfig(account, fifoUrl, authToken) {
  try {
    const r = await fetch(`${fifoUrl}/api/accounts/${account.id}/auto-login-config`, {
      headers: _authHeaders(authToken),
    });
    if (!r.ok) {
      log(`auto-login-config for ${account.bank_key} returned ${r.status}`);
      return null;
    }
    const cfg = await r.json();
    if (!cfg.has_credentials) {
      log(`${account.bank_key}: no saved credentials for auto-login.`);
      return null;
    }
    if (!cfg.username_selector || !cfg.password_selector) {
      log(`${account.bank_key}: login selectors missing from config; can't auto-login.`);
      return null;
    }
    return cfg;
  } catch (e) {
    log("auto-login-config fetch failed:", e.message);
    return null;
  }
}

const _sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Run a function in the page context of a tab and return its result.
async function _execInTab(tabId, func, arg) {
  try {
    const res = await chrome.scripting.executeScript({ target: { tabId }, func, args: [arg] });
    return res && res[0] ? res[0].result : null;
  } catch (e) {
    log("execInTab failed:", e.message);
    return null;
  }
}

// Resolve once the tab finishes loading (or after timeoutMs).
function _waitTabComplete(tabId, timeoutMs) {
  return new Promise((resolve) => {
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      chrome.tabs.onUpdated.removeListener(listener);
      resolve();
    };
    const listener = (id, info) => {
      if (id === tabId && info.status === "complete") finish();
    };
    chrome.tabs.onUpdated.addListener(listener);
    chrome.tabs.get(tabId).then((t) => { if (t && t.status === "complete") finish(); }).catch(() => {});
    setTimeout(finish, timeoutMs);
  });
}

// Open page probe / fill helpers run IN the page via _execInTab.
function _probePage(c) {
  const href = location.href.toLowerCase();
  const onSuccess = !!(c.success_url_substr && href.includes(c.success_url_substr.toLowerCase()));
  // OTP by URL: only the genuine challenge substrings (e.g. "authcode").
  const urlOtp = (c.otp_url_substrs || []).some((s) => href.includes(s.toLowerCase()));
  // OTP by field: ONLY a VISIBLE one-time-code input counts. Broad selectors
  // like [name*='code'] match unrelated hidden fields and false-positive.
  let fieldOtp = false;
  const otpEls = document.querySelectorAll("#tlpvt-otp-input, input[autocomplete='one-time-code']");
  for (const el of otpEls) { if (el.offsetParent !== null) { fieldOtp = true; break; } }
  const u = c.username_selector ? document.querySelector(c.username_selector) : null;
  const p = c.password_selector ? document.querySelector(c.password_selector) : null;
  return { href, onSuccess, urlOtp, fieldOtp, otp: (urlOtp || fieldOtp), hasForm: !!(u && p) };
}

// Fill username + password the framework-safe way. Angular/React track input
// via a value SETTER on the prototype and a native `input` event — a plain
// `el.value = x` bypasses that, leaving the reactive form "pristine" so the
// submit button stays disabled (exactly what we saw on ICICI). So: set via the
// native setter, then fire focus → input → change → keyup → blur to drive the
// validators and flip the button to enabled. Returns which fields were filled.
function _fillForm(c) {
  const nativeSet = (el, val) => {
    try {
      const desc = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), "value")
                || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
      if (desc && desc.set) desc.set.call(el, val); else el.value = val;
    } catch (_) { el.value = val; }
  };
  const fill = (el, val) => {
    if (!el) return false;
    el.focus();
    el.dispatchEvent(new Event("focus", { bubbles: true }));
    nativeSet(el, val);
    el.dispatchEvent(new InputEvent("input", { bubbles: true, data: val, inputType: "insertText" }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new KeyboardEvent("keyup", { bubbles: true, key: "x" }));
    el.dispatchEvent(new FocusEvent("blur", { bubbles: true }));
    return true;
  };
  const u = document.querySelector(c.username_selector);
  const p = document.querySelector(c.password_selector);
  const okU = fill(u, c.username);
  const okP = fill(p, c.password);
  return { filledUser: okU, filledPass: okP };
}

// Click submit, AFTER giving the framework a tick to enable the button.
// If it's still disabled (validation didn't take), clear the flag as a last
// resort and fall back to Enter / form.requestSubmit. Returns diagnostics.
function _clickSubmit(c) {
  const btn = c.submit_selector ? document.querySelector(c.submit_selector) : null;
  if (btn) {
    const wasDisabled = !!btn.disabled;
    if (btn.disabled) btn.disabled = false;   // belt-and-suspenders
    btn.click();
    return { clicked: true, wasDisabled, method: "button" };
  }
  const p = document.querySelector(c.password_selector);
  if (p) {
    p.dispatchEvent(new KeyboardEvent("keydown", { bubbles: true, key: "Enter", code: "Enter", keyCode: 13 }));
    if (p.form) {
      p.form.requestSubmit ? p.form.requestSubmit() : p.form.submit();
      return { clicked: true, wasDisabled: null, method: "form" };
    }
    return { clicked: true, wasDisabled: null, method: "enter" };
  }
  return { clicked: false };
}

// Drive the background tab: detect already-logged-in / OTP / login form, fill
// the saved creds, submit, then wait for success or OTP.
// Returns { status: "ok" | "otp" | "error" }.
async function _autoLoginInTab(tabId, cfg) {
  await _waitTabComplete(tabId, 45000);
  await _sleep(1500);

  let probe = await _execInTab(tabId, _probePage, cfg);
  if (probe && probe.otp) return { status: "otp" };
  if (probe && probe.onSuccess) return { status: "ok" }; // already logged in

  // Give a slow redirect one more chance to render the login form.
  if (!probe || !probe.hasForm) {
    await _sleep(3000);
    probe = await _execInTab(tabId, _probePage, cfg);
    if (probe && probe.otp) return { status: "otp" };
    if (probe && probe.onSuccess) return { status: "ok" };
  }
  if (!probe || !probe.hasForm) {
    log("auto-login: no login form found and not logged in.",
        `url=${probe && probe.href} onSuccess=${probe && probe.onSuccess}`,
        `hasForm=${probe && probe.hasForm} urlOtp=${probe && probe.urlOtp}`,
        `(success_url_substr='${cfg.success_url_substr}' username_selector='${cfg.username_selector}')`);
    return { status: "error" };
  }

  log("auto-login: filling saved credentials…");
  const fillRes = await _execInTab(tabId, _fillForm, cfg);
  log("auto-login: fill result:", JSON.stringify(fillRes));
  await _sleep(1200);   // let the framework run validators + enable the submit button
  const clickRes = await _execInTab(tabId, _clickSubmit, cfg);
  log("auto-login: submit click result:", JSON.stringify(clickRes));

  // Poll for the outcome. Log each probe so we can see exactly where BoA lands.
  const deadline = Date.now() + 45000;
  while (Date.now() < deadline) {
    await _sleep(1500);
    const st = await _execInTab(tabId, _probePage, cfg);
    if (!st) continue;
    log(`auto-login probe: url=${st.href} success=${st.onSuccess} urlOtp=${st.urlOtp} fieldOtp=${st.fieldOtp} hasForm=${st.hasForm}`);
    if (st.onSuccess) return { status: "ok" };
    if (st.otp) return { status: "otp" };
  }
  log("auto-login: timed out without reaching success or OTP.");
  return { status: "error" };
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
async function syncBank(account, fifoUrl, startUrl, timeoutMs, firstConnect = false, fifoTabId = null, authToken = null, allowAutoLogin = true) {
  log(`Syncing ${account.bank_key} (account #${account.id}) — opening ${startUrl} [firstConnect=${firstConnect}]`);
  await chrome.storage.local.set({ syncing: true });

  let tab;
  let backgroundWindow = null;       // only set for first-connect (visible) or as last-resort hidden window
  let usingExistingTab = false;      // true when we reused a tab the user already had open
  let openedBackgroundTab = false;   // true when WE opened a hidden tab in the real Chrome to auto-login
  let navListener;
  let _syncResult = null;            // populated on successful ingest, returned to caller

  // MV3 service workers are killed after ~30s of inactivity. Poll a chrome API
  // every 15s to keep the worker alive for the ENTIRE sync — including the
  // auto-login phase (which can take 45s+) BEFORE the scrape even starts. If the
  // worker dies mid-sync, sendResponse never fires and the page hangs until its
  // 5-min "Sync timed out". Start the keepalive up front, clear it in finally.
  const keepAlive = setInterval(() => chrome.storage.local.get("syncing"), 15000);
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
        // No existing tab. PRIMARY desktop path: open a hidden background tab
        // in the user's REAL Chrome and (if logged out) auto-login with the
        // saved password. The real Chrome holds the bank's device-trust cookie,
        // so this avoids the OTP that the server-side Playwright path ALWAYS
        // triggers (proven: even a full profile clone in a fresh browser gets
        // an OTP challenge). Only when no password is saved do we fall back.
        const loginCfg = allowAutoLogin
          ? await _fetchAutoLoginConfig(account, fifoUrl, authToken)
          : null;

        if (loginCfg && loginCfg.start_url) {
          log(`${account.bank_key} (#${account.id}): opening background tab for auto-login (${loginCfg.start_url})`);
          tab = await chrome.tabs.create({ url: loginCfg.start_url, active: false });
          openedBackgroundTab = true;

          const auto = await _autoLoginInTab(tab.id, loginCfg);
          if (auto.status === "otp") {
            // GENUINE OTP even in the real Chrome → manual re-login warranted.
            await _markNeedsLogin(account, fifoUrl, authToken);
            throw new Error(
              `${account.display_name || account.bank_key} needs you to re-login ` +
              `(your bank asked for a verification code).`
            );
          }
          if (auto.status === "error") {
            // Neither logged-in nor OTP within the timeout → soft, retryable.
            throw new Error(
              `${account.display_name || account.bank_key}: auto-login couldn't complete right now — will retry next cycle.`
            );
          }
          // auto.status === "ok": logged in (or already was). Kick off the scrape
          // by injecting content.js once; the navListener below re-injects on the
          // subsequent in-page navigations (overview → account activity).
          log(`${account.bank_key} (#${account.id}): auto-login OK — injecting content.js to scrape`);
          try {
            await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["content.js"] });
          } catch (e) {
            log("content.js inject after auto-login failed:", e.message);
          }
          // fall through to navListener + waitForScrapeResult below.
        } else {
          // No saved password (or auto-login disabled). Last resort: the old
          // server-side headless path. It will OTP if a real challenge appears,
          // but it's the only option left when we have no creds to type.
          const fb = await _headlessSyncFallback(account, fifoUrl, authToken);
          if (fb.status === "ok") {
            _syncResult = { newCount: fb.newCount, scrapedCount: fb.scrapedCount };
            await chrome.storage.local.set({ lastSync: new Date().toISOString(), lastError: null, syncing: false });
            return _syncResult;
          }
          if (fb.status === "relogin") {
            await _markNeedsLogin(account, fifoUrl, authToken);
            throw new Error(
              `${account.display_name || account.bank_key} needs you to re-login` +
              (fb.reason === "otp_required" ? " (your bank asked for a verification code)." : ".")
            );
          }
          if (fb.status === "error") {
            throw new Error(
              `${account.display_name || account.bank_key}: sync couldn't complete right now — will retry next cycle.`
            );
          }
          // fb.status === "skip": nothing we can do silently.
          throw new Error(
            `Open ${account.display_name || account.bank_key} in a tab (and log in) before clicking Sync now, ` +
            `or save your password so FIFO can sign in for you.`
          );
        }
      }
    }

    // (keepAlive started at the top of syncBank so it also covers auto-login.)

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
      // Live session is dead. Unified ladder step 2: before surfacing
      // "re-login required", try the headless password login. Only on a
      // silent/background sync (not first-connect, where the user is actively
      // logging in themselves).
      if (!firstConnect) {
        const fb = await _headlessSyncFallback(account, fifoUrl, authToken);
        if (fb.status === "ok") {
          _syncResult = { newCount: fb.newCount, scrapedCount: fb.scrapedCount };
          await chrome.storage.local.set({ lastSync: new Date().toISOString(), lastError: null, syncing: false });
          return _syncResult;
        }
        if (fb.status === "error") {
          // Live session was down AND the headless path hit a TECHNICAL error
          // (not a confirmed OTP). Don't false-alarm a re-login/OTP — leave the
          // account untouched and report a soft, retryable error.
          await setError(`${account.display_name || account.bank_key}: sync couldn't complete right now — will retry next cycle.`);
          return;
        }
        // fb.status === "relogin" or "skip" → genuine re-login needed; fall through.
      }
      // Genuine: live session dead and we can't auto-recover (OTP required, or
      // no saved password). Mark the account so the sidebar shows a re-login badge.
      await _markNeedsLogin(account, fifoUrl, authToken);
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
  log(`waitForScrapeResult: listening on tab #${tabId} (timeout ${timeoutMs / 1000}s)`);
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      chrome.runtime.onMessage.removeListener(handler);
      log(`waitForScrapeResult: TIMED OUT on tab #${tabId} after ${timeoutMs / 1000}s — content.js never sent a SCRAPE_RESULT.`);
      reject(new Error(`Timed out waiting for scrape result after ${timeoutMs / 1000}s`));
    }, timeoutMs);

    function handler(msg, sender) {
      if (msg.type !== "SCRAPE_RESULT" || sender.tab?.id !== tabId) return;
      const p = msg.payload;
      // content.js ships its accumulated nav-log inside the payload. The tab is
      // closed moments after a failure, so content.js's own async writes to
      // chrome.storage often don't flush in time — persisting from here (the
      // service worker, which outlives the tab) guarantees we see every step.
      if (Array.isArray(p.navLog)) {
        for (const line of p.navLog) _persistLog(line);
      }
      log(`waitForScrapeResult: got SCRAPE_RESULT on tab #${tabId} — error=${p.error || "none"} rows=${p.transactions ? p.transactions.length : 0}`);
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

// Persistent log ring buffer. MV3 service workers are killed after ~30s idle
// and their DevTools console does NOT survive a restart — so console-only logs
// vanish exactly when we need them (mid-sync). We mirror every log line into
// chrome.storage.local so it persists across worker restarts.
//
// Dump from the service-worker console with:
//   chrome.storage.local.get("fifoLog", d => console.log((d.fifoLog||[]).join("\n")))
// Clear with:
//   chrome.storage.local.remove("fifoLog")
let _logChain = Promise.resolve();
function _persistLog(line) {
  // Serialize writes through a promise chain so concurrent log() calls don't
  // clobber each other's read-modify-write. Reads fresh from storage each time,
  // so it keeps appending correctly even after a worker restart.
  _logChain = _logChain.then(async () => {
    try {
      const { fifoLog = [] } = await chrome.storage.local.get("fifoLog");
      fifoLog.push(line);
      if (fifoLog.length > 500) fifoLog.splice(0, fifoLog.length - 500);
      await chrome.storage.local.set({ fifoLog });
    } catch (_) { /* storage may be unavailable during teardown */ }
  });
}

function log(...args) {
  console.log("[FIFO]", ...args);
  try {
    const line = `[${new Date().toISOString()}] ` +
      args.map(a => (typeof a === "string" ? a : JSON.stringify(a))).join(" ");
    _persistLog(line);
  } catch (_) {}
}
