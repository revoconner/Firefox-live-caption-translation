// Drives extension/background.js against a stubbed WebExtension API and asserts the capture state machine.
// Run: node tests\extension_background.test.mjs
// Kept outside extension/ so it is never packaged into the built add-on.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const SOURCE = join(here, "..", "extension", "background.js");
const STOP_DELAY_MS = 1500; // must match background.js

const tick = () => new Promise((r) => setTimeout(r, 0));
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

// Objects created inside the vm realm have a different Object.prototype, which strict deepEqual rejects,
// so anything crossing back out is copied into this realm before being compared.
const plain = (o) => (o === undefined || o === null ? o : { ...o });

function makeWorld(initialSession = {}) {
    const world = {
        foreground: 1,
        tabs: new Map([[1, { id: 1 }], [2, { id: 2 }]]),
        tabMessages: [], // {tabId, msg}
        badges: new Map(),
        sessionStore: { ...initialSession },
        sockets: [],
        listeners: {},
    };

    const listener = (name) => {
        world.listeners[name] = [];
        return {
            addListener: (fn) => world.listeners[name].push(fn),
            removeListener: () => {},
        };
    };

    const browser = {
        storage: {
            session: {
                get: async (key) => (key in world.sessionStore ? { [key]: world.sessionStore[key] } : {}),
                set: async (obj) => Object.assign(world.sessionStore, obj),
            },
        },
        action: {
            onClicked: listener("clicked"),
            setBadgeText: async ({ tabId, text }) => { world.badges.set(tabId, text); },
            setBadgeBackgroundColor: async () => {},
            setTitle: async () => {},
        },
        tabs: {
            onActivated: listener("activated"),
            onUpdated: listener("updated"),
            onRemoved: listener("removed"),
            get: async (id) => {
                if (!world.tabs.has(id)) throw new Error("no such tab");
                return world.tabs.get(id);
            },
            query: async () => (world.tabs.has(world.foreground) ? [world.tabs.get(world.foreground)] : []),
            sendMessage: async (tabId, msg) => { world.tabMessages.push({ tabId, msg }); },
        },
        windows: {
            WINDOW_ID_NONE: -1,
            onFocusChanged: listener("focus"),
        },
        runtime: {
            onMessage: listener("message"),
        },
    };

    class FakeWebSocket {
        static CONNECTING = 0;
        static OPEN = 1;
        static CLOSING = 2;
        static CLOSED = 3;
        constructor(url) {
            this.url = url;
            this.readyState = 0;
            this.sent = [];
            world.sockets.push(this);
        }
        send(data) { this.sent.push(JSON.parse(data)); }
        close() {
            this.readyState = 3;
            if (this.onclose) this.onclose();
        }
        open() {
            this.readyState = 1;
            if (this.onopen) this.onopen();
        }
        deliver(obj) {
            if (this.onmessage) this.onmessage({ data: JSON.stringify(obj) });
        }
    }
    FakeWebSocket.prototype.CONNECTING = 0;
    FakeWebSocket.prototype.OPEN = 1;

    const context = vm.createContext({
        browser,
        WebSocket: FakeWebSocket,
        setTimeout,
        clearTimeout,
        setInterval,
        clearInterval,
        console,
    });
    vm.runInContext(readFileSync(SOURCE, "utf8"), context, { filename: "background.js" });

    // Helpers that drive the stubbed browser.
    world.click = async (tabId) => {
        for (const fn of world.listeners.clicked) await fn({ id: tabId });
        await tick();
    };
    world.activateTab = async (tabId) => {
        world.foreground = tabId;
        for (const fn of world.listeners.activated) await fn({ tabId });
        await tick();
    };
    world.update = async (tabId, change) => {
        for (const fn of world.listeners.updated) await fn(tabId, change);
        await tick();
    };
    world.remove = async (tabId) => {
        world.tabs.delete(tabId);
        for (const fn of world.listeners.removed) await fn(tabId);
        await tick();
    };
    world.message = async (msg, tabId, frameId = 0) => {
        let out;
        for (const fn of world.listeners.message) {
            const r = fn(msg, { tab: { id: tabId }, frameId });
            if (r) out = await r;
        }
        await tick();
        return out;
    };
    world.socket = () => world.sockets[world.sockets.length - 1];
    world.openSocket = async () => { world.socket().open(); await tick(); };
    world.backendSent = () => world.sockets.flatMap((s) => s.sent).map((m) => m.type).filter((t) => t !== "ping");
    world.tabSent = (tabId, type) => world.tabMessages.filter((m) => m.tabId === tabId && m.msg.type === type).map((m) => m.msg);
    world.clearLog = () => { world.tabMessages.length = 0; for (const s of world.sockets) s.sent.length = 0; };
    return world;
}

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test("enabling a foreground tab with a playing video starts capture", async () => {
    const w = makeWorld();
    await w.click(1);
    await w.openSocket();
    assert.equal(w.backendSent().includes("activate"), false, "no capture before a video plays");
    await w.message({ type: "playing", playing: true }, 1);
    assert.deepEqual(w.backendSent(), ["activate"]);
    assert.equal(w.badges.get(1), "ON");
});

test("an enabled tab in the background does not capture", async () => {
    const w = makeWorld();
    await w.click(1);
    await w.openSocket();
    await w.message({ type: "playing", playing: true }, 1);
    w.clearLog();
    await w.activateTab(2); // tab 2 is not enabled
    assert.deepEqual(plain(w.tabSent(1, "stream")[0]), { type: "stream", streaming: false });
    await wait(STOP_DELAY_MS + 100);
    assert.deepEqual(w.backendSent(), ["deactivate"]);
});

test("two enabled tabs hand the stream over on tab switch without extra clicks", async () => {
    const w = makeWorld();
    await w.click(1);
    await w.openSocket();
    await w.message({ type: "playing", playing: true }, 1);
    await w.click(2); // click while tab 1 is still foreground: tab 2 is enabled but not streaming
    assert.equal(w.badges.get(1), "ON", "enabling a second tab leaves the first one on");
    assert.equal(w.badges.get(2), "ON");
    w.clearLog();

    await w.activateTab(2);
    await w.message({ type: "playing", playing: true }, 2);
    assert.deepEqual(w.tabSent(2, "stream").at(-1).streaming, true);
    assert.deepEqual(w.tabSent(1, "stream").at(-1).streaming, false);
    assert.equal(w.backendSent().includes("deactivate"), false, "handover keeps capture running");

    w.clearLog();
    await w.activateTab(1); // and back again
    assert.deepEqual(w.tabSent(1, "stream").at(-1).streaming, true);
    assert.equal(w.backendSent().includes("deactivate"), false);
});

test("reload keeps the tab enabled, repaints the badge and waits for the video", async () => {
    const w = makeWorld();
    await w.click(1);
    await w.openSocket();
    await w.message({ type: "playing", playing: true }, 1);
    w.clearLog();

    w.badges.set(1, ""); // Firefox clears per-tab badge text on navigation
    await w.update(1, { status: "loading" });
    await w.update(1, { status: "complete" });
    assert.equal(w.badges.get(1), "ON", "badge is restored after navigation");

    const q = await w.message({ type: "query" }, 1);
    assert.deepEqual(plain(q), { active: true, streaming: true, connected: true });

    await wait(STOP_DELAY_MS + 100);
    assert.deepEqual(w.backendSent(), ["deactivate"], "capture idles until a video plays again");

    w.clearLog();
    await w.message({ type: "playing", playing: true }, 1);
    assert.deepEqual(w.backendSent(), ["activate"], "a new video on the same tab resumes capture");
});

test("a brief pause does not stop capture", async () => {
    const w = makeWorld();
    await w.click(1);
    await w.openSocket();
    await w.message({ type: "playing", playing: true }, 1);
    w.clearLog();
    await w.message({ type: "playing", playing: false }, 1);
    await wait(200);
    await w.message({ type: "playing", playing: true }, 1); // resumed within the debounce
    await wait(STOP_DELAY_MS + 100);
    assert.deepEqual(w.backendSent(), [], "no churn on a seek or short pause");
});

test("a sustained pause stops capture", async () => {
    const w = makeWorld();
    await w.click(1);
    await w.openSocket();
    await w.message({ type: "playing", playing: true }, 1);
    w.clearLog();
    await w.message({ type: "playing", playing: false }, 1);
    await wait(STOP_DELAY_MS + 100);
    assert.deepEqual(w.backendSent(), ["deactivate"]);
});

test("one frame of several keeps the tab playing", async () => {
    const w = makeWorld();
    await w.click(1);
    await w.openSocket();
    await w.message({ type: "playing", playing: true }, 1, 0);
    await w.message({ type: "playing", playing: true }, 1, 7);
    w.clearLog();
    await w.message({ type: "playing", playing: false }, 1, 0); // outer frame has no video, iframe still plays
    await wait(STOP_DELAY_MS + 100);
    assert.deepEqual(w.backendSent(), []);
});

test("turning a tab off leaves the other enabled tab working", async () => {
    const w = makeWorld();
    await w.click(1);
    await w.click(2);
    await w.openSocket();
    await w.activateTab(2);
    await w.message({ type: "playing", playing: true }, 2);
    w.clearLog();

    await w.click(1); // turn tab 1 off while tab 2 streams
    assert.equal(w.badges.get(1), "");
    assert.deepEqual(plain(w.tabSent(1, "active").at(-1)), { type: "active", active: false });
    await wait(STOP_DELAY_MS + 100);
    assert.deepEqual(w.backendSent(), [], "tab 2 is unaffected");
    assert.equal(w.socket().readyState, 1, "socket stays open");
});

test("turning the last tab off closes the socket", async () => {
    const w = makeWorld();
    await w.click(1);
    await w.openSocket();
    await w.message({ type: "playing", playing: true }, 1);
    await w.click(1);
    assert.equal(w.socket().readyState, 3, "socket closed");
    assert.equal(w.backendSent().includes("deactivate"), true);
});

test("closing a streaming tab stops capture", async () => {
    const w = makeWorld();
    await w.click(1);
    await w.openSocket();
    await w.message({ type: "playing", playing: true }, 1);
    w.clearLog();
    await w.remove(1);
    assert.equal(w.socket().readyState, 3);
});

test("losing browser focus keeps captions running", async () => {
    const w = makeWorld();
    await w.click(1);
    await w.openSocket();
    await w.message({ type: "playing", playing: true }, 1);
    w.clearLog();
    for (const fn of w.listeners.focus) await fn(-1); // WINDOW_ID_NONE
    await tick();
    await wait(STOP_DELAY_MS + 100);
    assert.deepEqual(w.backendSent(), [], "alt-tabbing out of Firefox does not stop the stream");
});

test("a restarted event page restores the enabled tabs from session storage", async () => {
    const first = makeWorld();
    await first.click(1);
    await first.click(2);
    const persisted = { ...first.sessionStore };

    // Simulates Firefox suspending and restarting the event page with session storage already populated.
    const second = makeWorld(persisted);
    second.tabs.delete(2); // tab 2 was closed while suspended
    second.foreground = 1;
    await second.activateTab(1);
    assert.equal(second.badges.get(1), "ON", "tab 1 is still enabled");
    assert.equal(second.badges.has(2), false, "the closed tab is dropped");
    await second.openSocket();
    await second.message({ type: "playing", playing: true }, 1);
    assert.deepEqual(second.backendSent(), ["activate"]);
});

test("the pong state corrects drift against the backend", async () => {
    const w = makeWorld();
    await w.click(1);
    await w.openSocket();
    await w.message({ type: "playing", playing: true }, 1);
    w.clearLog();
    w.socket().deliver({ type: "pong", state: "idle" }); // backend lost the capture somehow
    await tick();
    assert.deepEqual(w.backendSent(), ["activate"], "capture is re-requested");
});

test("caption events only reach the streaming tab", async () => {
    const w = makeWorld();
    await w.click(1);
    await w.click(2);
    await w.openSocket();
    await w.message({ type: "playing", playing: true }, 1);
    w.clearLog();
    w.socket().deliver({ type: "final", id: 1, text: "hola", lang: "es-ES", needs_translation: true });
    await tick();
    assert.equal(w.tabSent(1, "final").length, 1);
    assert.equal(w.tabSent(2, "final").length, 0);
});

let failed = 0;
for (const [name, fn] of tests) {
    try {
        await fn();
        console.log(`  ok   ${name}`);
    } catch (e) {
        failed++;
        console.log(`  FAIL ${name}\n       ${e.message.split("\n").join("\n       ")}`);
    }
}
console.log(`\n${tests.length - failed}/${tests.length} passed`);
process.exit(failed ? 1 : 0);
