/**
 * FIFO Bank Connector — Popup Script
 *
 * Lets the user trigger a bank sync directly from the extension icon,
 * without needing to open the FIFO web app first.
 */

const FIFO_API_BASE = "https://finance-demo-486722019708.us-central1.run.app";
const FIFO_APP_URL  = "https://finance-software.pages.dev";

const statusEl = document.getElementById("status");

function setStatus(type, text) {
  statusEl.className = `status ${type}`;
  statusEl.textContent = text;
}

// Wire up bank buttons
document.querySelectorAll(".bank-btn[data-bank]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const bank = btn.dataset.bank;
    startSync(bank);
  });
});

// Open FIFO app link
document.getElementById("open-fifo").addEventListener("click", (e) => {
  e.preventDefault();
  chrome.tabs.create({ url: FIFO_APP_URL });
  window.close();
});

async function startSync(bank) {
  setStatus("info", "Opening your bank — please log in…");

  // Send to background
  try {
    await chrome.runtime.sendMessage({
      type:    "START_BANK_SYNC",
      bank,
      apiBase: FIFO_API_BASE,
      // No fifoTabId — background will handle without a FIFO tab open
    });
  } catch (err) {
    setStatus("error", `Error: ${err.message}`);
    return;
  }

  // Listen for status updates from background
  chrome.runtime.onMessage.addListener(function listener(msg) {
    if (msg.type !== "FIFO_SYNC_STATUS") return;

    switch (msg.status) {
      case "logging_in":
        setStatus("info", "Waiting for login…");
        break;
      case "scraping":
        setStatus("info", "Reading your transactions…");
        break;
      case "saving":
        setStatus("info", msg.message || "Saving transactions…");
        break;
      case "done":
        setStatus("success", `✓ Synced ${msg.data?.count ?? 0} transactions!`);
        chrome.runtime.onMessage.removeListener(listener);
        break;
      case "error":
        setStatus("error", msg.message || "Something went wrong.");
        chrome.runtime.onMessage.removeListener(listener);
        break;
    }
  });
}
