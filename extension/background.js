// Owns the single WebSocket to the local backend and the one active tab. Toolbar click toggles the active tab.
// Switching the active tab keeps the socket and the capture running; only toggling off closes it.
const BACKEND_URL = "ws://127.0.0.1:8765";
const RECONNECT_MS = 2000;
const PING_MS = 15000;

let activeTabId = null;
let ws = null;
let reconnectTimer = null;
let pingTimer = null;

// The event page can be terminated and restarted by Firefox; the active tab survives in session storage.
function persistActive() {
    browser.storage.session.set({ activeTabId }).catch(() => {});
}

browser.storage.session.get("activeTabId").then((r) => {
    if (activeTabId === null && typeof r.activeTabId === "number") {
        activeTabId = r.activeTabId;
        setBadge(activeTabId, true);
        connect();
    }
}).catch(() => {});

function setBadge(tabId, on) {
    if (tabId === null) return;
    browser.action.setBadgeText({ tabId, text: on ? "ON" : "" }).catch(() => {});
    if (on) browser.action.setBadgeBackgroundColor({ tabId, color: "#2e7d32" }).catch(() => {});
}

function sendToTab(tabId, msg) {
    if (tabId === null) return;
    browser.tabs.sendMessage(tabId, msg).catch(() => {});
}

function wsSend(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

function connect() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
    ws = new WebSocket(BACKEND_URL);
    ws.onopen = () => {
        sendToTab(activeTabId, { type: "backend", connected: true });
        if (activeTabId !== null) wsSend({ type: "activate" });
        if (!pingTimer) pingTimer = setInterval(() => wsSend({ type: "ping" }), PING_MS);
    };
    ws.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch { return; }
        if (msg.type === "pong") {
            if (msg.state === "idle" && activeTabId !== null) wsSend({ type: "activate" });
            return;
        }
        sendToTab(activeTabId, msg);
    };
    ws.onclose = () => {
        ws = null;
        if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
        sendToTab(activeTabId, { type: "backend", connected: false });
        if (activeTabId !== null) scheduleReconnect();
    };
    ws.onerror = () => {};
}

function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        if (activeTabId !== null) connect();
    }, RECONNECT_MS);
}

function disconnect() {
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
    wsSend({ type: "deactivate" });
    if (ws) { ws.close(); ws = null; }
}

function deactivate() {
    const prev = activeTabId;
    activeTabId = null;
    persistActive();
    disconnect();
    setBadge(prev, false);
    sendToTab(prev, { type: "active", active: false });
}

function activate(tabId) {
    const prev = activeTabId;
    if (prev !== null && prev !== tabId) {
        setBadge(prev, false);
        sendToTab(prev, { type: "active", active: false });
    }
    activeTabId = tabId;
    persistActive();
    setBadge(tabId, true);
    sendToTab(tabId, { type: "active", active: true });
    if (ws && ws.readyState === WebSocket.OPEN) {
        sendToTab(tabId, { type: "backend", connected: true });
        wsSend({ type: "activate" });
    } else {
        connect();
    }
}

browser.action.onClicked.addListener((tab) => {
    if (tab.id === activeTabId) deactivate();
    else activate(tab.id);
});

browser.tabs.onRemoved.addListener((tabId) => {
    if (tabId === activeTabId) deactivate();
});

browser.runtime.onMessage.addListener((msg, sender) => {
    if (!msg) return undefined;
    if (msg.type === "query") {
        const active = !!(sender.tab && sender.tab.id === activeTabId);
        return Promise.resolve({ active, connected: !!(ws && ws.readyState === WebSocket.OPEN) });
    }
    if (msg.type === "keepalive") {
        // Content script pings while active so the event page is not suspended and the socket survives.
        if (activeTabId !== null && (!ws || ws.readyState === WebSocket.CLOSED)) connect();
        return Promise.resolve({ ok: true });
    }
    return undefined;
});
