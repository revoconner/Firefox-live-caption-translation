// Options page shown inside the add-on manager. Every change is saved immediately to browser.storage.local.
(() => {
    const form = document.getElementById("form");
    const saved = document.getElementById("saved");
    const preview = document.getElementById("preview");
    let savedTimer = null;

    function read() {
        const s = {};
        for (const el of form.elements) {
            if (!el.name) continue;
            s[el.name] = el.type === "checkbox" ? el.checked : el.value;
        }
        return lctNormalize(s);
    }

    function fill(s) {
        for (const el of form.elements) {
            if (!el.name || !(el.name in s)) continue;
            if (el.type === "checkbox") el.checked = s[el.name]; else el.value = s[el.name];
        }
        for (const out of form.querySelectorAll("output[data-for]")) out.value = form.elements[out.dataset.for].value;
        paintPreview(s);
    }

    function paintPreview(s) {
        const outline = lctHexToRgba(s.outlineColor, s.outlineOpacity);
        preview.style.setProperty("--lct-bg", lctHexToRgba(s.bgColor, s.bgOpacity));
        preview.style.setProperty("--lct-fg", s.fontColor);
        preview.style.setProperty("--lct-outline", outline);
        preview.style.fontSize = `${s.fontSize}px`;
        for (const el of preview.children) {
            el.style.background = `var(--lct-bg)`;
            el.style.color = `var(--lct-fg)`;
            el.style.textShadow = `1px 1px 0 ${outline}, -1px 1px 0 ${outline}, 1px -1px 0 ${outline}, -1px -1px 0 ${outline}, 0 0 3px ${outline}`;
        }
    }

    function save() {
        const s = read();
        paintPreview(s);
        browser.storage.local.set(s).then(() => {
            saved.textContent = "Saved";
            clearTimeout(savedTimer);
            savedTimer = setTimeout(() => { saved.textContent = ""; }, 1500);
        });
    }

    form.addEventListener("input", (ev) => {
        const out = form.querySelector(`output[data-for="${ev.target.name}"]`);
        if (out) out.value = ev.target.value;
        save();
    });
    form.addEventListener("change", save);

    document.getElementById("reset").addEventListener("click", () => {
        fill(LCT_DEFAULTS);
        save();
    });

    browser.storage.local.get(null).then((raw) => fill(lctNormalize(raw)));
})();
