/**
 * FIFO Bridge Content Script
 *
 * Injected into FIFO web app pages (Cloudflare Pages, localhost).
 * Acts as a two-way relay between the FIFO page (window.postMessage)
 * and the background service worker (chrome.runtime.sendMessage).
 *
 * Also stamps window.__FIFO_EXTENSION so the page can detect the extension.
 */

// Let the FIFO page know the extension is present
window.__FIFO_EXTENSION = { version: "1.0.0" };

// ── Page → Extension ─────────────────────────────────────────────────────────

window.addEventListener("message", (event) => {
  // Only accept messages from the same window
  if (event.source !== window) return;

  const msg = event.data;
  if (!msg || typeof msg !== "object") return;

  if (msg.type === "FIFO_SMART_CONNECT") {
    // Relay start request to background
    chrome.runtime.sendMessage({
      type:    "START_BANK_SYNC",
      bank:    msg.bank,
      apiBase: msg.apiBase,
    }).catch(console.error);
  }
});

// ── Extension → Page ─────────────────────────────────────────────────────────

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.type === "FIFO_SYNC_STATUS") {
    // Forward sync status back to the FIFO page
    window.postMessage(msg, "*");
  }
});
