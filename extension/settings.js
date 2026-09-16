// Shared between the options page and the content script: default settings and helpers.
const LCT_DEFAULTS = {
    showLive: true,          // show the italic in progress transcription
    showUntranslated: false, // show a finalised non English line before its translation arrives
    removeDelayMs: 500,      // how long a completed caption stays after its audio ended
    bgColor: "#000000",
    bgOpacity: 65,           // percent
    fontSize: 24,            // px at a 1280 px wide player, scales with the player
    fontColor: "#ffffff",
    outlineColor: "#000000",
    outlineOpacity: 100,     // percent
};

function lctHexToRgba(hex, opacityPercent) {
    const m = /^#?([0-9a-f]{6})$/i.exec(hex || "");
    const v = m ? parseInt(m[1], 16) : 0;
    const a = Math.max(0, Math.min(100, Number(opacityPercent))) / 100;
    return `rgba(${(v >> 16) & 255}, ${(v >> 8) & 255}, ${v & 255}, ${a})`;
}

function lctNormalize(raw) {
    const s = { ...LCT_DEFAULTS, ...(raw || {}) };
    s.showLive = !!s.showLive;
    s.showUntranslated = !!s.showUntranslated;
    s.removeDelayMs = Math.max(0, Math.min(60000, Number(s.removeDelayMs) || 0));
    s.fontSize = Math.max(8, Math.min(120, Number(s.fontSize) || LCT_DEFAULTS.fontSize));
    s.bgOpacity = Math.max(0, Math.min(100, Number(s.bgOpacity)));
    s.outlineOpacity = Math.max(0, Math.min(100, Number(s.outlineOpacity)));
    for (const k of ["bgColor", "fontColor", "outlineColor"]) {
        if (!/^#[0-9a-f]{6}$/i.test(s[k])) s[k] = LCT_DEFAULTS[k];
    }
    return s;
}
