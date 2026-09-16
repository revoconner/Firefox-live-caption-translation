// Overlays captions on the playing video in this frame while the tab is active.
(() => {
    if (window.__liveCaptionLoaded) return;
    window.__liveCaptionLoaded = true;

    const MAX_LINES = 2;
    let active = false;
    let overlay = null;
    let partialEl = null;
    let linesEl = null;
    let statusEl = null;
    let finals = [];
    let partial = "";
    let tick = null;
    let currentVideo = null;

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
    }

    function removeOverlay() {
        if (overlay) overlay.remove();
        overlay = partialEl = linesEl = statusEl = null;
    }

    function isPlaying(v) {
        return !v.paused && !v.ended && v.readyState >= 2 && !v.muted && v.volume > 0;
    }

    function pickVideo() {
        let best = null;
        let bestArea = 0;
        for (const v of document.querySelectorAll("video")) {
            if (!isPlaying(v)) continue;
            const r = v.getBoundingClientRect();
            const area = r.width * r.height;
            if (area > bestArea) { best = v; bestArea = area; }
        }
        return best;
    }

    function render() {
        if (!overlay) return;
        linesEl.replaceChildren(...finals.slice(-MAX_LINES).map((f) => {
            const d = document.createElement("div");
            d.className = "lct-line" + (f.needs_translation && !f.translation ? " lct-pending" : "");
            d.textContent = f.translation || f.text;
            if (f.translation) d.title = f.text;
            return d;
        }));
        partialEl.textContent = partial;
        partialEl.style.display = partial ? "" : "none";
    }

    function position() {
        if (!overlay) return;
        const v = pickVideo();
        currentVideo = v;
        if (!v) { overlay.style.display = "none"; return; }
        const r = v.getBoundingClientRect();
        overlay.style.display = "";
        overlay.style.left = `${r.left}px`;
        overlay.style.width = `${r.width}px`;
        overlay.style.top = `${r.top + r.height * 0.78}px`;
        overlay.style.maxHeight = `${r.height * 0.22}px`;
        overlay.style.fontSize = `${Math.max(14, Math.min(34, r.width / 32))}px`;
    }

    function start() {
        active = true;
        finals = [];
        partial = "";
        ensureOverlay();
        setStatus("connecting");
        render();
        position();
        if (!tick) tick = setInterval(position, 250);
        window.addEventListener("resize", position);
        document.addEventListener("scroll", position, true);
    }

    function stop() {
        active = false;
        if (tick) { clearInterval(tick); tick = null; }
        window.removeEventListener("resize", position);
        document.removeEventListener("scroll", position, true);
        removeOverlay();
    }

    function setStatus(text) {
        if (statusEl) {
            statusEl.textContent = text;
            statusEl.style.display = text ? "" : "none";
        }
    }

    browser.runtime.onMessage.addListener((msg) => {
        if (!msg || typeof msg !== "object") return;
        switch (msg.type) {
            case "active":
                if (msg.active) start(); else stop();
                break;
            case "backend":
                if (active) setStatus(msg.connected ? "" : "backend offline");
                break;
            case "status":
                if (active) setStatus(msg.state === "capturing" ? "" : `${msg.state}: ${msg.detail || ""}`);
                break;
            case "partial":
                if (!active) return;
                partial = msg.text;
                render();
                break;
            case "final":
                if (!active) return;
                partial = "";
                finals.push({ id: msg.id, text: msg.text, lang: msg.lang, needs_translation: msg.needs_translation, translation: null });
                if (finals.length > 20) finals = finals.slice(-20);
                render();
                break;
            case "translation": {
                if (!active) return;
                const f = finals.find((x) => x.id === msg.id);
                if (f) { f.translation = msg.text; render(); }
                break;
            }
        }
    });

    browser.runtime.sendMessage({ type: "query" }).then((r) => {
        if (r && r.active) {
            start();
            setStatus(r.connected ? "" : "backend offline");
        }
    }).catch(() => {});
})();
