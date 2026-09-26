const browser = globalThis.browser ?? globalThis.chrome;
const $ = (id) => document.getElementById(id);

// Accept "example.com", "http://example.com", "example.com/", etc. and
// return a canonical "http://example.com" with no trailing slash.
// Defaults to http:// because ytfinall serves plain HTTP on port 6842.
// Users on HTTPS type https:// themselves and it's respected.
function normalizeServerUrl(raw) {
    let s = (raw || '').trim();
    if (!s) return '';
    if (!/^https?:\/\//i.test(s)) s = 'http://' + s;
    return s.replace(/\/+$/, '');
}

document.addEventListener('DOMContentLoaded', async () => {
    const data = await browser.storage.local.get(['jellyfinToken', 'username', 'backendUrl']);
    // Repair any URL stored before normalization existed.
    if (data.backendUrl) {
        const fixed = normalizeServerUrl(data.backendUrl);
        if (fixed && fixed !== data.backendUrl) {
            data.backendUrl = fixed;
            await browser.storage.local.set({ backendUrl: fixed });
        }
    }
    if (data.jellyfinToken && data.username) {
        showSendView(data.username);
        try {
            const tabs = await browser.tabs.query({ active: true, currentWindow: true });
            if (tabs && tabs[0] && tabs[0].url && /youtube\.com|youtu\.be/.test(tabs[0].url)) {
                $('videoUrl').value = tabs[0].url;
            }
        } catch (_) {}
    }
    $('loginBtn').addEventListener('click', doLogin);
    $('sendBtn').addEventListener('click', doSend);
    $('logoutBtn').addEventListener('click', doLogout);
});

function setStatus(el, msg, cls) {
    el.textContent = msg;
    el.className = 'status ' + (cls || '');
}

function showSendView(username) {
    $('view-login').classList.add('hidden');
    $('view-send').classList.remove('hidden');
    $('whoami').textContent = username;
}

function showLoginView() {
    $('view-send').classList.add('hidden');
    $('view-login').classList.remove('hidden');
}

async function doLogin() {
    const backendUrl = normalizeServerUrl($('backendUrl').value);
    const username = $('username').value.trim();
    const password = $('password').value;
    if (!backendUrl || !username || !password) {
        setStatus($('loginStatus'), 'Fill in all three fields.', 'err');
        return;
    }
    setStatus($('loginStatus'), 'Logging in...');
    $('loginBtn').disabled = true;
    try {
        const resp = await fetch(backendUrl + '/api/extension-login', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username: username, password: password })
        });
        const result = await resp.json();
        if (resp.ok && result.token) {
            await browser.storage.local.set({
                jellyfinToken: result.token,
                username: result.username || username,
                backendUrl: backendUrl
            });
            setStatus($('loginStatus'), 'Success!', 'ok');
            showSendView(result.username || username);
        } else {
            setStatus($('loginStatus'), 'Failed: ' + (result.error || 'Unknown'), 'err');
        }
    } catch (e) {
        setStatus($('loginStatus'), 'Connection error: ' + e.message, 'err');
    } finally {
        $('loginBtn').disabled = false;
    }
}

async function doSend() {
    const url = $('videoUrl').value.trim();
    if (!url) {
        setStatus($('sendStatus'), 'Paste a URL first.', 'err');
        return;
    }
    const data = await browser.storage.local.get(['jellyfinToken', 'backendUrl']);
    if (!data.jellyfinToken || !data.backendUrl) {
        setStatus($('sendStatus'), 'Not logged in.', 'err');
        return;
    }
    $('sendBtn').disabled = true;
    setStatus($('sendStatus'), 'Sending...');
    try {
        const resp = await fetch(data.backendUrl + '/api/download', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url: url, token: data.jellyfinToken })
        });
        const result = await resp.json();
        if (resp.ok) {
            setStatus($('sendStatus'), 'Queued as ' + result.type_detected + '.', 'ok');
            $('videoUrl').value = '';
        } else {
            setStatus($('sendStatus'), 'Error: ' + (result.error || 'Unknown'), 'err');
        }
    } catch (e) {
        setStatus($('sendStatus'), 'Connection error: ' + e.message, 'err');
    } finally {
        $('sendBtn').disabled = false;
    }
}

async function doLogout() {
    await browser.storage.local.remove(['jellyfinToken', 'username', 'backendUrl']);
    showLoginView();
    setStatus($('loginStatus'), 'Logged out.');
}
