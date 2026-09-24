# Installing ytfinall Connector on Chrome

The Chrome Web Store does not accept browser extensions that facilitate
downloading YouTube videos, so ytfinall Connector is not listed there.
This is a policy restriction, not a technical one — the extension works
perfectly on Chrome once installed.

This guide walks you through installing it manually. The whole process
takes about two minutes and only has to be done once. Chrome will keep
the extension installed across browser restarts.

---

## What You Need

- Chrome (any recent version)
- A ytfinall server running and reachable from your computer
  (see [README.md](README.md) for server setup)
- Your Jellyfin username and password

You do **not** need to install Node, Python, or any build tools. The
extension is plain JavaScript that Chrome loads directly from the folder.

---

## Step 1 — Download the Extension

### Option A — Download a zip (easiest)

1. Go to the [Releases page](https://github.com/jnracreates/ytfinall/releases)
2. Find the latest release
3. Under **Assets**, download `ytfinall-connector-<version>.zip`
4. Extract the zip to a permanent folder on your computer. Suggestions:
   - Windows: `C:\Users\<your-name>\ytfinall-extension\`
   - macOS: `/Users/<your-name>/ytfinall-extension/`
   - Linux: `/home/<your-name>/ytfinall-extension/`

**Do not** put the folder in Downloads or a temp directory. Chrome
loads the extension from this folder every time it starts. If you
delete the folder, the extension stops working.

### Option B — Clone the repository (if you prefer git)

```bash
git clone https://github.com/jnracreates/ytfinall.git
```

The extension lives in the `ytfinall-extension/` folder inside the repo.

---

## Step 2 — Open Chrome's Extension Manager

In the address bar, type:

```
chrome://extensions/
```

Press Enter.

You'll see a page listing your installed extensions. There will be a
toggle labeled **Developer mode** in the top-right corner. Click it to
turn it on.

Once Developer mode is on, three new buttons appear in the top-left:
**Load unpacked**, **Pack extension**, and **Update**.

---

## Step 3 — Load the Extension

1. Click **Load unpacked**
2. Chrome opens a file picker. Navigate to the folder you extracted in
   Step 1
3. Select the folder (single-click it) and click **Select Folder**
   (Windows) or **Open** (macOS/Linux)

**Important:** Select the folder that contains `manifest.json`
directly. If you extracted the zip and got a nested folder like
`ytfinall-connector-1.0.2/ytfinall-connector-1.0.2/`, use the inner one.

After selecting, Chrome shows a new card on the extensions page labeled
**ytfinall Connector**. It has a toggle (enabled by default) and a small
list of details.

If Chrome shows a red **Errors** button on the card, click it to see
what went wrong. Common causes:
- You selected the wrong folder (missing `manifest.json`)
- The zip extracted into a nested folder structure

Fix and click **Load unpacked** again.

---

## Step 4 — Pin the Extension to Your Toolbar

Chrome hides new extensions behind a puzzle-piece icon by default. To
make ytfinall easy to access:

1. Click the **puzzle-piece** icon in the top-right of the browser
2. Find **ytfinall Connector** in the list
3. Click the **pin icon** next to it

The extension icon now appears on your toolbar permanently.

---

## Step 5 — Log In

1. Click the ytfinall icon in your toolbar
2. A small popup appears with a login form
3. Fill in:
   - **Server URL:** the address of your ytfinall server, e.g.
     `http://192.168.1.50:6842` or `https://youtube.yourdomain.com`
   - **Username:** your Jellyfin username
   - **Password:** your Jellyfin password
4. Click **Log In**

If the login succeeds, the popup switches to a send form and shows
"Logged in as your-username."

If it fails:
- Double-check the server URL. On your local network, use the LAN IP
  (like `192.168.1.50`), not `localhost`. On a phone, `localhost` means
  the phone itself.
- Verify the server is reachable: open `http://<your-server>:6842` in
  a browser tab. You should see the ytfinall login page.
- Check your Jellyfin credentials by logging into Jellyfin's web UI
  with the same username and password.

---

## Step 6 — Test It

1. Go to any YouTube video
2. Right-click the video thumbnail or title link
3. In the context menu, click **Send to ytfinall**
4. You should see a small notification or hear nothing at all — the
   extension sends silently in the background
5. Open your ytfinall server's dashboard. The URL should appear in the
   **Active downloads** panel with a progress bar
6. When the download finishes, the video appears in your Jellyfin
   library (may take a few minutes for Jellyfin to scan)

If the right-click menu doesn't show **Send to ytfinall**, the extension
isn't loaded correctly. Go back to Step 3.

---

## Using It Day-to-Day

**Right-click on a video** to send just that video.

**Right-click on a channel name** to subscribe to the whole channel.

**Right-click on a playlist link** to download the playlist.

**On a page with no YouTube link** — right-click empty space on the page
and choose **Send to ytfinall**. The extension will send whatever URL
the current tab is showing.

---

## Keeping It Updated

Chrome does not auto-update manually loaded extensions. To update:

1. Download the new version of the extension zip from the releases page
2. Extract it to the **same folder** you used before (overwrite existing files)
3. Go to `chrome://extensions/`
4. Click the **circular refresh arrow** on the ytfinall Connector card

The extension reloads with the new code. Your login stays intact — the
session token is stored in Chrome's extension storage, not in the
extension files.

If the release notes mention a version bump, check the number on the
extension card matches. If it doesn't, the refresh didn't take — try
removing and re-adding the extension.

---

## Troubleshooting

### "Could not load manifest" or "Manifest is not valid JSON"

You selected the wrong folder. Make sure `manifest.json` is directly
inside the folder you chose. Not:
```
ytfinall-connector-1.0.2/
  ytfinall-connector-1.0.2/     ← wrong level
    manifest.json
```
But:
```
ytfinall-connector-1.0.2/
  manifest.json                  ← right level
  background.js
  popup.html
  popup.js
  icons/
```

### The extension loads but right-click doesn't show the menu

1. Go to `chrome://extensions/` and confirm the toggle is on
2. Right-click the extension icon → **Manage Extension**
3. Check that **Site access** is set to "On all sites" (the extension
   needs this to add the context menu on any page)

### "Session expired" or login fails with 401

Your Jellyfin token has expired. Click the extension icon, click
**Log Out**, and log back in with your credentials.

This happens when:
- Jellyfin is restarted
- Your password is changed
- An admin revokes your session

Just log back in to get a fresh token.

### "Connection error" when sending

Your server isn't reachable from this computer. Test:
- Open the server URL directly in a browser tab. Does the ytfinall
  login page load?
- If not, the server might be down, or you're on a different network
- If on a laptop traveling away from home, use a VPN like Tailscale,
  or a public URL (Cloudflare Tunnel, reverse proxy) that points at
  your server

### It worked, but stopped working after Chrome updated

Chrome occasionally resets extension permissions after major updates.
Go to `chrome://extensions/`, find ytfinall Connector, click **Details**,
and check that permissions are granted. If not, re-enable the extension
by toggling it off and back on.

### I want to remove the extension

1. `chrome://extensions/`
2. Find ytfinall Connector
3. Click **Remove**

You can also delete the extracted folder from your computer. Your
ytfinall server data is unaffected — removing the extension only
removes the browser-side tool.

---

## Why Isn't It on the Chrome Web Store?

The Chrome Web Store prohibits extensions that facilitate downloading
YouTube videos. This is a policy limitation, not a technical one, and
it affects every browser extension with this functionality. The same
extension is available on:

- **Microsoft Edge Add-ons:** [install link](https://microsoftedge.microsoft.com/addons/detail/ytfinall-connector/hdfpngaekagheekoajkjhdjoomppmlhj)
- **Firefox Add-ons (AMO):** pending review
- **Chrome:** manual install (this document)

If you use Chrome and want a one-click install from a store, you can
install the Edge version by switching to Edge for browsing YouTube —
or keep using Chrome with the manual install described above.

---

## Getting Help

If something in this guide doesn't work, open an issue:

https://github.com/jnracreates/ytfinall/issues

Include:
- What step you're on
- What you expected to happen
- What actually happened
- A screenshot if possible
