// Owns the single WebSocket to the local backend and the one active tab. Toolbar click toggles the active tab.
const BACKEND_URL = "ws://127.0.0.1:8765";

let activeTabId = null;
let ws = null;
let reconnectTimer = null;

function setBadge(tabId, on) {
    if (tabId === null) return;
    browser.action.setBadgeText({ tabId, text: on ? "ON" : "" }).catch(() => {});
    if (on) browser.action.setBadgeBackgroundColor({ tabId, color: "#2e7d32" }).catch(() => {});
}

function sendToTab(msg) {
    if (activeTabId === null) return;
    browser.tabs.sendMessage(activeTabId, msg).catch(() => {});
}

function connect() {
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
    ws = new WebSocket(BACKEND_URL);
    ws.onopen = () => {
        sendToTab({ type: "backend", connected: true });
        if (activeTabId !== null) ws.send(JSON.stringify({ type: "activate" }));
    };
    ws.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch { return; }
        sendToTab(msg);
    };
    ws.onclose = () => {
        ws = null;
        sendToTab({ type: "backend", connected: false });
        if (activeTabId !== null) scheduleReconnect();
    };
    ws.onerror = () => {};
}

function scheduleReconnect() {
    if (reconnectTimer) return;
    reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        if (activeTabId !== null) connect();
    }, 2000);
}

function disconnect() {
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "deactivate" }));
    if (ws) { ws.close(); ws = null; }
}

function deactivate() {
    const prev = activeTabId;
    activeTabId = null;
    disconnect();
    if (prev !== null) {
        setBadge(prev, false);
        browser.tabs.sendMessage(prev, { type: "active", active: false }).catch(() => {});
    }
}

function activate(tabId) {
    if (activeTabId !== null && activeTabId !== tabId) deactivate();
    activeTabId = tabId;
    setBadge(tabId, true);
    sendToTab({ type: "active", active: true });
    connect();
}

browser.action.onClicked.addListener((tab) => {
    if (tab.id === activeTabId) deactivate();
    else activate(tab.id);
});

browser.tabs.onRemoved.addListener((tabId) => {
    if (tabId === activeTabId) deactivate();
});

browser.runtime.onMessage.addListener((msg, sender) => {
    if (msg && msg.type === "query") {
        const active = sender.tab && sender.tab.id === activeTabId;
        return Promise.resolve({ active, connected: !!(ws && ws.readyState === WebSocket.OPEN) });
    }
    return undefined;
});
