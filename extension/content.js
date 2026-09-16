// Overlays captions on the playing video in this frame.
//
// Two independent states: the tab is enabled by the user, and this tab is the one currently streaming
// (enabled plus foreground). The overlay only draws while streaming. The frame reports whether it has a
// playing video so the backend can idle when nothing is playing. Appearance and behaviour come from the
// settings page (settings.js is loaded before this file) and apply live when changed.
(() => {
    if (window.__liveCaptionLoaded) return;
    window.__liveCaptionLoaded = true;

    const MAX_LINES = 2;
    const TICK_MS = 250;
    const KEEPALIVE_MS = 10000;
    const REFERENCE_WIDTH = 1280; // the player width at which fontSize applies unscaled
    const QUIET_MS = 1500; // no partial, final or translation for this long means the speech has stopped

    let settings = lctNormalize({});
    let enabled = false;
    let streaming = false;
    let overlay = null;
    let partialEl = null;
    let linesEl = null;
    let statusEl = null;
    let finals = []; // {id, text, translation, needs_translation, timer, replacedBy}
    let partial = "";
    let live = null; // {text, stable, lang} English for the in progress line, when the speech is not English
    let tick = null;
    let keepalive = null;
    let quietTimer = null;
    let reportedPlaying = false;

    function ensureOverlay() {
        if (overlay) return;
        overlay = document.createElement("div");
        overlay.id = "lct-overlay";
        linesEl = document.createElement("div");
        linesEl.className = "lct-lines";
        partialEl = document.createElement("div");
        partialEl.className = "lct-partial";
        statusEl = document.createElement("div");
        statusEl.className = "lct-status";
        overlay.append(statusEl, linesEl, partialEl);
        (document.body || document.documentElement).appendChild(overlay);
        applyStyle();
    }

    function removeOverlay() {
        if (overlay) overlay.remove();
        overlay = partialEl = linesEl = statusEl = null;
    }

    function applyStyle() {
        if (!overlay) return;
        overlay.style.setProperty("--lct-bg", lctHexToRgba(settings.bgColor, settings.bgOpacity));
        overlay.style.setProperty("--lct-fg", settings.fontColor);
        overlay.style.setProperty("--lct-outline", lctHexToRgba(settings.outlineColor, settings.outlineOpacity));
    }

    function playable(v) {
        return !v.paused && !v.ended && v.readyState >= 2 && !v.muted && v.volume > 0;
    }

    function pickVideo() {
        let best = null;
        let bestArea = 0;
        for (const v of document.querySelectorAll("video")) {
            if (!playable(v)) continue;
            const r = v.getBoundingClientRect();
            const area = r.width * r.height;
            if (area > bestArea) { best = v; bestArea = area; }
        }
        return best;
    }

    function setStatus(text) {
        if (!statusEl) return;
        statusEl.textContent = text;
        statusEl.style.display = text ? "" : "none";
    }

    // A final is complete once it has its translation, or immediately when none is needed.
    function isComplete(f) {
        return !f.needs_translation || !!f.translation;
    }

    // A caption replaced by a merged one stays visible until the merged caption is ready, so the screen never blanks.
    function visible(f) {
        if (f.replacedBy) {
            const r = finals.find((x) => x.id === f.replacedBy);
            return !r || !visible(r);
        }
        return isComplete(f) || settings.showUntranslated;
    }

    function renderPartial() {
        const useLive = settings.liveTranslation && live && partial;
        const useSource = settings.showLive && partial;
        if (useLive) {
            const stable = document.createElement("span");
            stable.textContent = live.stable;
            const rest = document.createElement("span");
            rest.className = "lct-unstable";
            rest.textContent = live.text.slice(live.stable.length);
            partialEl.replaceChildren(stable, rest);
            partialEl.style.display = "";
        } else if (useSource) {
            partialEl.textContent = partial;
            partialEl.style.display = "";
        } else {
            partialEl.textContent = "";
            partialEl.style.display = "none";
        }
    }

    function render() {
        if (!overlay) return;
        linesEl.replaceChildren(...finals.filter(visible).slice(-MAX_LINES).map((f) => {
            const d = document.createElement("div");
            d.className = "lct-line" + (isComplete(f) ? "" : " lct-pending");
            d.textContent = f.translation || f.text;
            if (f.translation) d.title = f.text;
            return d;
        }));
        renderPartial();
    }

    function dropFinal(f) {
        const i = finals.indexOf(f);
        if (i >= 0) finals.splice(i, 1);
        render();
    }

    // A caption stays until something replaces it. Two things do: a newer caption becoming complete (its
    // translation landed, or it needed none) retires every older caption after the delay, and the speech
    // going quiet retires everything after the delay. A caption is never retired by its own completion.
    function scheduleRemoval(f) {
        if (f.timer) return;
        f.timer = setTimeout(() => dropFinal(f), settings.removeDelayMs);
    }

    function retireOlderThan(f) {
        for (const g of finals) {
            if (g !== f && g.id < f.id) scheduleRemoval(g);
        }
    }

    function retireAll() {
        for (const g of finals) scheduleRemoval(g);
    }

    // Every caption event restarts the quiet window; when it expires the speech is considered stopped.
    function touch() {
        clearTimeout(quietTimer);
        quietTimer = setTimeout(retireAll, QUIET_MS);
    }

    function onComplete(f) {
        retireOlderThan(f);
        touch();
    }

    function clearFinals() {
        clearTimeout(quietTimer);
        quietTimer = null;
        for (const f of finals) clearTimeout(f.timer);
        finals = [];
        live = null;
    }

    // Once a merged caption is complete, the fragments it replaced go immediately.
    function dropReplacedBy(f) {
        for (const g of finals.filter((x) => x.replacedBy === f.id)) {
            clearTimeout(g.timer);
            finals.splice(finals.indexOf(g), 1);
        }
    }

    function report(isPlaying) {
        if (isPlaying === reportedPlaying) return;
        reportedPlaying = isPlaying;
        browser.runtime.sendMessage({ type: "playing", playing: isPlaying }).catch(() => {});
    }

    function position() {
        const v = pickVideo();
        report(!!v);
        if (!overlay) return;
        if (!v) { overlay.style.display = "none"; return; }
        const r = v.getBoundingClientRect();
        overlay.style.display = "";
        overlay.style.left = `${r.left}px`;
        overlay.style.width = `${r.width}px`;
        overlay.style.top = `${r.top + r.height * 0.78}px`;
        overlay.style.maxHeight = `${r.height * 0.22}px`;
        overlay.style.fontSize = `${Math.max(10, settings.fontSize * (r.width / REFERENCE_WIDTH))}px`;
    }

    function startTick() {
        if (!tick) tick = setInterval(position, TICK_MS);
    }

    function stopTick() {
        if (tick) { clearInterval(tick); tick = null; }
    }

    // Reports keep flowing while the tab is enabled, so the backend knows when a video starts in a tab
    // the user turned on earlier. The overlay and the keepalive only run while this tab is streaming.
    function setEnabled(on) {
        if (enabled === on) return;
        enabled = on;
        if (on) {
            startTick();
            window.addEventListener("resize", position);
            document.addEventListener("scroll", position, true);
            position();
        } else {
            setStreaming(false);
            stopTick();
            window.removeEventListener("resize", position);
            document.removeEventListener("scroll", position, true);
            report(false);
        }
    }

    function setStreaming(on) {
        if (streaming === on) {
            if (on) position();
            return;
        }
        streaming = on;
        if (on) {
            clearFinals();
            partial = "";
            ensureOverlay();
            render();
            position();
            if (!keepalive) keepalive = setInterval(() => browser.runtime.sendMessage({ type: "keepalive" }).catch(() => {}), KEEPALIVE_MS);
        } else {
            if (keepalive) { clearInterval(keepalive); keepalive = null; }
            clearFinals();
            partial = "";
            removeOverlay();
        }
    }

    browser.runtime.onMessage.addListener((msg) => {
        if (!msg || typeof msg !== "object") return;
        switch (msg.type) {
            case "active":
                setEnabled(!!msg.active);
                break;
            case "stream":
                setStreaming(!!msg.streaming);
                if (msg.streaming && msg.connected === false) setStatus("backend offline");
                break;
            case "backend":
                if (streaming) setStatus(msg.connected ? "" : "backend offline");
                break;
            case "status":
                if (streaming) setStatus(msg.state === "capturing" ? "" : `${msg.state}: ${msg.detail || ""}`);
                break;
            case "partial":
                if (!streaming) return;
                partial = msg.text;
                touch();
                render();
                break;
            case "live_translation":
                if (!streaming) return;
                live = { text: msg.text, stable: msg.stable, lang: msg.lang };
                touch();
                render();
                break;
            case "final": {
                if (!streaming) return;
                partial = "";
                live = null;
                const f = { id: msg.id, text: msg.text, lang: msg.lang, needs_translation: msg.needs_translation, translation: null, timer: null, replacedBy: null };
                for (const id of msg.replaces || []) {
                    const g = finals.find((x) => x.id === id);
                    if (g) { g.replacedBy = f.id; clearTimeout(g.timer); g.timer = null; }
                }
                finals.push(f);
                while (finals.filter((x) => !x.replacedBy).length > MAX_LINES) {
                    const oldest = finals.find((x) => !x.replacedBy);
                    clearTimeout(oldest.timer);
                    finals.splice(finals.indexOf(oldest), 1);
                }
                if (isComplete(f)) { dropReplacedBy(f); onComplete(f); } else touch();
                render();
                break;
            }
            case "translation": {
                if (!streaming) return;
                const f = finals.find((x) => x.id === msg.id);
                if (f) {
                    f.translation = msg.text;
                    dropReplacedBy(f);
                    onComplete(f);
                    render();
                }
                break;
            }
        }
    });

    function loadSettings() {
        return browser.storage.local.get(null).then((raw) => {
            settings = lctNormalize(raw);
            applyStyle();
            position();
            render();
        }).catch(() => {});
    }

    browser.storage.onChanged.addListener((changes, area) => {
        if (area === "local") loadSettings();
    });

    // A fresh document after a reload or navigation asks the background for the tab's standing state.
    loadSettings().then(() => browser.runtime.sendMessage({ type: "query" })).then((r) => {
        if (!r || !r.active) return;
        setEnabled(true);
        if (r.streaming) {
            setStreaming(true);
            if (!r.connected) setStatus("backend offline");
        }
    }).catch(() => {});
})();
