// FIFO Sync — Bridge Content Script
// Runs on the FIFO demo page (localhost + production).
// 1. Signals the page that the extension is installed (three methods for reliability).
// 2. Listens for FIFO_SMART_CONNECT postMessages from the page.
// 3. Routes sync requests to the background service worker.
// 4. Sends FIFO_SYNC_STATUS updates back to the page.

(function () {
  // ── Signal the page that the extension is installed ──────────────────
  //
  // Content scripts run in an isolated JS world — their `window` is NOT the
  // page's window. We use three methods so at least one always works:
  //
  // Method 1: DOM attribute — content scripts share the real DOM with the
  //   page, so setAttribute works immediately without any script injection.
  document.documentElement.setAttribute("data-fifo-ext", "1");

  // Method 2: Custom DOM event — fires before any user interaction so the
  //   page can set a flag the moment bridge.js loads.
  document.dispatchEvent(new CustomEvent("fifo-extension-ready", { bubbles: false }));

  // ── Listen for sync requests from the FIFO demo page ─────────────────
  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    const msg = event.data;
    if (!msg || msg.type !== "FIFO_SMART_CONNECT") return;

    const { bank, apiBase } = msg;
    log(`Smart Connect requested for bank: ${bank}, apiBase: ${apiBase}`);

    sendStatus("logging_in");

    // Forward the user's auth token so background.js can call the now-authenticated
    // backend endpoints (/api/accounts and /api/smart-connect/extension-ingest).
    // Content scripts share the page's localStorage, so this just works.
    const authToken = localStorage.getItem("fifoAuthToken") || null;

    chrome.runtime.sendMessage(
      { type: "SMART_CONNECT_SYNC", bank_key: bank, apiBase, authToken },
      (response) => {
        if (chrome.runtime.lastError) {
          sendStatus("error", chrome.runtime.lastError.message);
          return;
        }
        if (!response) {
          sendStatus("error", "No response from extension background.");
          return;
        }
        if (response.status === "scraping") {
          sendStatus("scraping");
        } else if (response.ok) {
          sendStatus("done", null, { count: response.count, received: response.received });
        } else {
          sendStatus("error", response.error || "Sync failed.");
        }
      }
    );
  });

  function sendStatus(status, message, data) {
    window.postMessage({ type: "FIFO_SYNC_STATUS", status, message, data }, "*");
  }

  function log(...args) {
    console.log("[FIFO bridge]", ...args);
  }
})();
