# ytfinall config folder

This folder is mounted into the container at `/config`.

## cookies.txt

If a file named `cookies.txt` is present here, yt‑dlp will use it
automatically. This helps with:

- Age‑restricted videos
- Members‑only content
- Content that requires being logged into YouTube
- Avoiding "Sign in to confirm you're not a bot" errors

### How to export cookies.txt

1. Install a browser extension like **"Get cookies.txt LOCALLY"**
   (Chrome, Firefox, Edge).
2. Open Private/Incognito window.
3. Log into YouTube in that browser. - Use an account you would never login to youtube.
4. In that same tab and window, navigate to https://www.youtube.com/robots.txt
5. Click the Export icon in your extension, make sure Export Format is "Netscape" and click "Export As"
6. Close window.
6. Save the exported file as `cookies.txt` in this folder.
7. Restart ytfinall: `docker compose restart ytfinall`.


Cookies expire. If downloads start failing with auth errors, re‑export.
