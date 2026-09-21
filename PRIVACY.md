# Privacy Policy — ytfinall Connector

**Last updated: September 20, 2026**

## Summary

The ytfinall Connector browser extension does not collect, transmit, or sell any user data to the developer or to any third party. All communication is between the user's browser and a server the user themselves hosts.

## What the extension does

The extension lets a user send YouTube video, channel, and playlist URLs to a self-hosted ytfinall server for downloading into their personal Jellyfin media library. The user configures the server URL themselves during setup.

## Information the extension handles

The extension handles the following information, all of which stays between the user's browser and the user's own server:

- **Jellyfin username and password.** Entered once by the user during login. Sent only to the user's own ytfinall server to obtain a session token. Never stored by the extension. Never sent to the developer or to any third party.
- **Jellyfin session token.** Returned by the user's ytfinall server after login. Stored locally in the browser's extension storage (`storage.local`) so the user stays logged in between sessions. Sent only to the user's own server when queuing a download.
- **ytfinall server URL.** Entered by the user during setup. Stored locally in `storage.local`. Used only to address requests to the user's own server.
- **YouTube URLs the user chooses to send.** Transmitted only to the user's own server when the user explicitly triggers a send (right-click menu or popup button).
- **Active tab URL.** Read when the user opens the extension popup, to pre-fill the send field if the user is already on a YouTube page. Never stored, never transmitted.

## What the extension does not do

- It does not collect analytics, telemetry, or usage statistics.
- It does not transmit any data to the developer.
- It does not transmit any data to Google, Microsoft, Mozilla, or any third party.
- It does not track browsing history.
- It does not read or modify page content.
- It does not use cookies of its own.
- It does not use remote code. All JavaScript is bundled inside the extension package.

## Data storage and retention

The only data stored by the extension is the user's server URL, Jellyfin username, and session token. All three are stored in the browser's local extension storage (`storage.local`), which stays on the user's device. The user can erase this data at any time by clicking "Log Out" in the extension popup, or by removing the extension from their browser.

## Data sharing

No data is shared with anyone. The extension has no backend server controlled by the developer.

## Children's privacy

The extension is not directed at children and does not knowingly collect any information from children.

## Changes to this policy

If this policy changes, the updated version will be posted at this URL with a new "Last updated" date.

## Contact

For questions about this policy, open an issue at
https://github.com/jnracreates/ytfinall/issues
