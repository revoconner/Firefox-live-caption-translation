// Owns the single WebSocket to the local backend and the set of tabs the user turned on.
//
// A tab stays on until the user clicks the button again, across reloads and navigation. Capture only runs
// for the one enabled tab that is in the foreground and has a playing video, so several tabs can be on at
// once without fighting over the audio stream. Switching tabs moves the caption stream with no clicks.
const BACKEND_URL = "ws://127.0.0.1:8765";
const RECONNECT_MS = 2000;
const PING_MS = 15000;
const STOP_DELAY_MS = 1500; // ride out pauses and seeks instead of stopping capture immediately

const enabled = new Set(); // tab ids the user turned on
const playing = new Map(); // tabId -> Set of frame ids reporting a playing video
let streamTabId = null; // the enabled tab currently receiving captions
let capturing = false; // whether the backend was last told to capture
let ws = null;
let reconnectTimer = null;
let pingTimer = null;
let stopTimer = null;

// Firefox can suspend and restart this event page at any time, so the enabled set lives in session storage.
let ready = null;

function init() {
    if (!ready) {
        ready = browser.storage.session.get("enabledTabs").then(async (r) => {
            const ids = Array.isArray(r.enabledTabs) ? r.enabledTabs : [];
            for (const id of ids) {
                try {
                    await browser.tabs.get(id); // drop tabs closed while we were suspended
                    enabled.add(id);
                    paint(id);
                } catch { /* gone */ }
            }
        }).catch(() => {});
    }
    return ready;
}

function persist() {
    browser.storage.session.set({ enabledTabs: [...enabled] }).catch(() => {});
}

// Per-tab badge and title are cleared by Firefox whenever the tab navigates, so this is re-applied on update.
function paint(tabId) {
    const on = enabled.has(tabId);
    browser.action.setBadgeText({ tabId, text: on ? "ON" : "" }).catch(() => {});
    if (on) browser.action.setBadgeBackgroundColor({ tabId, color: "#2e7d32" }).catch(() => {});
    browser.action.setTitle({ tabId, title: on ? "Live captions on for this tab: click to turn off" : "Live captions: click to turn on for this tab" }).catch(() => {});
}

function sendToTab(tabId, msg) {
    if (tabId === null || tabId === undefined) return;
    browser.tabs.sendMessage(tabId, msg).catch(() => {});
}

function wsSend(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

function connect() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
    ws = new WebSocket(BACKEND_URL);
    ws.onopen = () => {
        sendToTab(streamTabId, { type: "backend", connected: true });
        capturing = false;
        apply();
        if (!pingTimer) pingTimer = setInterval(() => wsSend({ type: "ping" }), PING_MS);
    };
    ws.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch { return; }
        if (msg.type === "pong") {
            // Self-heal if our idea of the backend state drifted from the backend's.
            const backendCapturing = msg.state === "capturing";
            if (backendCapturing !== capturing) {
                capturing = backendCapturing;
                apply();
            }
            return;
        }
        sendToTab(streamTabId, msg);
    };
    ws.onclose = () => {
        ws = null;
        capturing = false;
        if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
        sendToTab(streamTabId, { type: "backend", connected: false });
        if (enabled.size) scheduleReconnect();
    };
    ws.onerror = () => {};
}

function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        if (enabled.size) connect();
    }, RECONNECT_MS);
}

function disconnect() {
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
    if (stopTimer) { clearTimeout(stopTimer); stopTimer = null; }
    wsSend({ type: "deactivate" });
    capturing = false;
    if (ws) { ws.close(); ws = null; }
}

function isPlaying(tabId) {
    const frames = playing.get(tabId);
    return !!(frames && frames.size);
}

// Bring the backend in line with what the current stream tab needs.
function apply() {
    const want = streamTabId !== null && isPlaying(streamTabId);
    if (want) {
        if (stopTimer) { clearTimeout(stopTimer); stopTimer = null; }
        if (!capturing) {
            capturing = true;
            connect();
            wsSend({ type: "activate" });
        }
        return;
    }
    if (capturing && !stopTimer) {
        stopTimer = setTimeout(() => {
            stopTimer = null;
            if (streamTabId !== null && isPlaying(streamTabId)) return;
            capturing = false;
            wsSend({ type: "deactivate" });
        }, STOP_DELAY_MS);
    }
}

// The stream tab is the enabled tab that is active in the most recently focused window.
async function pickStreamTab() {
    if (!enabled.size) return null;
    try {
        const tabs = await browser.tabs.query({ active: true, lastFocusedWindow: true });
        const t = tabs[0];
        return t && enabled.has(t.id) ? t.id : null;
    } catch {
        return null;
    }
}

async function refresh() {
    const next = await pickStreamTab();
    if (next !== streamTabId) {
        const prev = streamTabId;
        streamTabId = next;
        if (prev !== null) sendToTab(prev, { type: "stream", streaming: false });
        if (next !== null) sendToTab(next, { type: "stream", streaming: true, connected: !!(ws && ws.readyState === WebSocket.OPEN) });
    }
    if (enabled.size && !ws) connect();
    if (!enabled.size) disconnect();
    apply();
}

browser.action.onClicked.addListener(async (tab) => {
    await init();
    if (enabled.has(tab.id)) {
        enabled.delete(tab.id);
        playing.delete(tab.id);
        sendToTab(tab.id, { type: "active", active: false });
    } else {
        enabled.add(tab.id);
        sendToTab(tab.id, { type: "active", active: true });
    }
    persist();
    paint(tab.id);
    await refresh();
});

browser.tabs.onActivated.addListener(async () => {
    await init();
    await refresh();
});

browser.windows.onFocusChanged.addListener(async (windowId) => {
    // Ignore the browser losing focus entirely: the video keeps playing and the user may still be listening.
    if (windowId === browser.windows.WINDOW_ID_NONE) return;
    await init();
    await refresh();
});

// Navigation clears per-tab badge state and tears down content scripts, but the tab stays enabled.
browser.tabs.onUpdated.addListener(async (tabId, change) => {
    await init();
    if (!enabled.has(tabId)) return;
    if (change.status === "loading") {
        playing.delete(tabId);
        apply();
    }
    paint(tabId);
}, { properties: ["status"] });

browser.tabs.onRemoved.addListener(async (tabId) => {
    await init();
    if (!enabled.delete(tabId)) return;
    playing.delete(tabId);
    persist();
    await refresh();
});

browser.runtime.onMessage.addListener((msg, sender) => {
    if (!msg) return undefined;
    const tabId = sender.tab && sender.tab.id;
    const frameId = sender.frameId || 0;

    if (msg.type === "query") {
        return init().then(() => ({
            active: enabled.has(tabId),
            streaming: tabId === streamTabId,
            connected: !!(ws && ws.readyState === WebSocket.OPEN),
        }));
    }
    if (msg.type === "playing") {
        return init().then(() => {
            if (!enabled.has(tabId)) return { ok: false };
            let frames = playing.get(tabId);
            if (!frames) { frames = new Set(); playing.set(tabId, frames); }
            if (msg.playing) frames.add(frameId); else frames.delete(frameId);
            apply();
            return { ok: true };
        });
    }
    if (msg.type === "keepalive") {
        // Firefox suspends this event page after ~30s without an extension event, which would kill the
        // socket. The streaming tab pings so the page stays resident while captions are flowing.
        return init().then(() => {
            if (enabled.size && (!ws || ws.readyState === WebSocket.CLOSED)) connect();
            return { ok: true };
        });
    }
    return undefined;
});

init();
