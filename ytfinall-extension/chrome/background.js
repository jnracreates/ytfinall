// Use the 'browser' namespace, falling back to 'chrome' for compatibility.
const browser = globalThis.browser ?? globalThis.chrome;

// --- Context Menu (Desktop only) ---
// This feature is not supported on mobile browsers.
if (browser.contextMenus) {
  browser.runtime.onInstalled.addListener(() => {
    browser.contextMenus.create({
      id: "sendToYtfinall",
      title: "Send to ytfinall",
      contexts: ["link", "video", "page"]
    });
  });

  browser.contextMenus.onClicked.addListener(async (info) => {
    if (info.menuItemId === "sendToYtfinall") {
      const url = info.linkUrl || info.videoUrl || info.pageUrl;
      if (url) await queueDownload(url);
    }
  });
}

// --- Shared Download Function ---
// This is used by both the desktop context menu and the mobile popup.
async function queueDownload(url) {
  const data = await browser.storage.local.get(['jellyfinToken', 'backendUrl']);
  if (!data.jellyfinToken || !data.backendUrl) {
    console.error("Not logged in. Open the extension popup first.");
    return { ok: false, error: "Not logged in" };
  }

  try {
    const resp = await fetch(`${data.backendUrl}/api/download`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, token: data.jellyfinToken })
    });
    const result = await resp.json();
    if (resp.ok) {
      console.log("Queued:", result);
      return { ok: true, result };
    }
    console.error("Backend error:", result);
    return { ok: false, error: result.error || "Backend error" };
  } catch (e) {
    console.error("Connection failed:", e);
    return { ok: false, error: e.message };
  }
}

// Expose the function to the popup script
self.queueDownload = queueDownload;
