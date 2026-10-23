const DEFAULT_FIFO = "http://localhost:8000";

const syncBtn   = document.getElementById("syncBtn");
const saveBtn   = document.getElementById("saveBtn");
const urlInput  = document.getElementById("fifoUrl");
const statusDot = document.getElementById("statusDot");
const statusTxt = document.getElementById("statusText");
const errorBox  = document.getElementById("errorBox");
const lastSyncEl= document.getElementById("lastSync");

// ── Load saved state on open ──────────────────────────────────────────────
chrome.storage.local.get(["fifoUrl", "lastSync", "lastError", "syncing"], (data) => {
  urlInput.value = data.fifoUrl || DEFAULT_FIFO;
  renderStatus(data);
});

// ── Save URL ──────────────────────────────────────────────────────────────
saveBtn.addEventListener("click", () => {
  const url = urlInput.value.trim().replace(/\/$/, "");
  chrome.storage.local.set({ fifoUrl: url }, () => {
    saveBtn.textContent = "Saved ✓";
    setTimeout(() => saveBtn.textContent = "Save", 1500);
  });
});

// ── Manual sync ───────────────────────────────────────────────────────────
syncBtn.addEventListener("click", () => {
  syncBtn.disabled = true;
  syncBtn.textContent = "Syncing…";
  setStatus("yellow", "Syncing…");

  chrome.runtime.sendMessage({ type: "SYNC_NOW" }, (resp) => {
    syncBtn.disabled = false;
    syncBtn.textContent = "↻ Sync Now";
    chrome.storage.local.get(["lastSync", "lastError", "syncing"], renderStatus);
  });
});

// ── Render status ─────────────────────────────────────────────────────────
function renderStatus(data) {
  if (data.syncing) {
    setStatus("yellow", "Syncing…");
    errorBox.style.display = "none";
    lastSyncEl.textContent = "";
    return;
  }
  if (data.lastError) {
    setStatus("red", "Sync failed");
    errorBox.style.display = "block";
    errorBox.textContent = data.lastError;
  } else if (data.lastSync) {
    setStatus("green", "Connected");
    errorBox.style.display = "none";
  } else {
    setStatus("gray", "Not synced yet");
    errorBox.style.display = "none";
  }
  if (data.lastSync) {
    const d = new Date(data.lastSync);
    lastSyncEl.textContent = `Last sync: ${d.toLocaleDateString()} ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
  }
}

function setStatus(color, text) {
  statusDot.className = `dot dot-${color}`;
  statusTxt.textContent = text;
}
