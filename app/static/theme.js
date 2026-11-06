"use strict";
/*
 * Look & feel for AutoDate (same theme set as the unraid-home project).
 * Loaded in <head> so the saved look is applied before the page paints (no flash).
 *
 * look  = { theme, radius, density, font, shadow, width, mode }   - saved on the server (/data/ui.json)
 * mode  = auto | light | dark; each browser can override it (localStorage), the server value is the default.
 * A theme has a light and a dark palette: bg, bg2 (optional gradient), surface, text, muted, border, accent.
 */
(function () {
  const P = (bg, surface, text, muted, border, accent, extra) => ({ bg, surface, text, muted, border, accent, ...extra });
  const THEMES = [
    { id: "default", name: "AutoDate",
      light: P("#f4f6f8", "#ffffff", "#17202a", "#5d6b7a", "#dde3ea", "#0f766e"),
      dark: P("#11161c", "#1a222b", "#e6ebf0", "#93a1b0", "#2b3643", "#2dd4bf") },
    { id: "unraid", name: "Unraid orange",
      light: P("#f4f5f7", "#ffffff", "#1b1f24", "#667085", "#dde1e6", "#e8590c"),
      dark: P("#111317", "#1a1d23", "#e6e8eb", "#8b94a3", "#2c313a", "#f76707") },
    { id: "midnight", name: "Midnight",
      light: P("#eef2fb", "#ffffff", "#14213d", "#5c6b8a", "#d5ddf0", "#3b82f6"),
      dark: P("#0b1020", "#141b34", "#e6ebff", "#8a96bd", "#25305a", "#5aa2ff", { bg2: "#131a33" }) },
    { id: "nord", name: "Nord",
      light: P("#eceff4", "#ffffff", "#2e3440", "#5e6b81", "#d8dee9", "#5e81ac"),
      dark: P("#2e3440", "#3b4252", "#eceff4", "#a3adc2", "#4c566a", "#88c0d0") },
    { id: "dracula", name: "Dracula",
      light: P("#f8f8f2", "#ffffff", "#282a36", "#6272a4", "#e1e1d8", "#7c4dff"),
      dark: P("#282a36", "#343746", "#f8f8f2", "#a6aecc", "#44475a", "#bd93f9") },
    { id: "solarized", name: "Solarized",
      light: P("#fdf6e3", "#fffbf0", "#586e75", "#93a1a1", "#eee8d5", "#b58900"),
      dark: P("#002b36", "#073642", "#93a1a1", "#657b83", "#0f4452", "#2aa198") },
    { id: "forest", name: "Forest",
      light: P("#eef3ec", "#ffffff", "#1d2b1f", "#607264", "#d3dfd0", "#2f9e44"),
      dark: P("#0f1a12", "#17241a", "#e3efe4", "#8aa38d", "#264030", "#51cf66", { bg2: "#16261a" }) },
    { id: "rose", name: "Rosé",
      light: P("#fdf1f4", "#ffffff", "#3a1f2b", "#8a6572", "#f3d5de", "#e64980"),
      dark: P("#1b1116", "#26181f", "#f6e7ec", "#b58b99", "#3d2530", "#f06595") },
    { id: "glass", name: "Glass", glass: true,
      light: P("#dfe9f5", "#ffffff", "#1b2230", "#55627a", "#ffffff", "#3b82f6", { bg2: "#f5dfe9" }),
      dark: P("#0d1224", "#1b2038", "#eef1ff", "#a2acd0", "#3a4170", "#7aa2ff", { bg2: "#2a1038" }) },
    { id: "contrast", name: "High contrast", border: 2,
      light: P("#ffffff", "#ffffff", "#000000", "#333333", "#000000", "#0044cc"),
      dark: P("#000000", "#000000", "#ffffff", "#dddddd", "#ffffff", "#ffd400") },
    { id: "terminal", name: "Terminal", font: "mono",
      light: P("#f2f5ee", "#f8faf4", "#1a2a14", "#4b6a3c", "#9fb88f", "#2f7d12"),
      dark: P("#050805", "#0a120a", "#7dff6a", "#3fa032", "#1f4d1a", "#39ff14") },
  ];
  const FONTS = {
    system: 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
    rounded: 'ui-rounded, "SF Pro Rounded", Nunito, "Segoe UI", system-ui, sans-serif',
    serif: 'Georgia, "Times New Roman", serif',
    mono: "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
  };
  const DENSITY = { compact: { pad: "10px 12px", gap: "10px" }, comfortable: { pad: "14px 16px", gap: "14px" },
                    spacious: { pad: "20px 22px", gap: "20px" } };
  const WIDTH = { standard: "1100px", wide: "1600px", full: "100%" };
  const DEFAULTS = { theme: "default", radius: 10, density: "comfortable", font: "", shadow: "none", width: "wide", mode: "auto" };
  // status colours stay recognisable in every theme
  const STATUS = {
    light: { warn: "#b45309", "warn-soft": "#fef3c7", bad: "#b42318", "bad-soft": "#fde8e6", ok: "#15803d" },
    dark: { warn: "#fbbf24", "warn-soft": "#3a2e10", bad: "#f87171", "bad-soft": "#3b1a1a", ok: "#4ade80" },
  };

  const store = {
    get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { if (v == null) localStorage.removeItem(k); else localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
  };
  const root = document.documentElement;
  const mq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  let look = { ...DEFAULTS };
  let applied = [];

  const isMode = (v) => v === "auto" || v === "light" || v === "dark";
  const modePref = () => { const v = store.get("ad-mode"); return isMode(v) ? v : (isMode(look.mode) ? look.mode : "auto"); };
  const effective = () => { const p = modePref(); return p === "auto" ? (mq && mq.matches ? "dark" : "light") : p; };

  function luminance(hex) {
    const m = /^#([0-9a-f]{6})$/i.exec(hex || "");
    if (!m) return 0;
    const [r, g, b] = [0, 2, 4].map((i) => parseInt(m[1].slice(i, i + 2), 16) / 255)
      .map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  }

  function apply(next, opts) {
    look = { ...DEFAULTS, ...(next || {}) };
    const t = THEMES.find((x) => x.id === look.theme) || THEMES[0];
    const mode = effective();
    const p = t[mode];
    root.dataset.mode = mode;
    root.dataset.theme = t.id;
    root.style.colorScheme = mode;
    for (const n of applied) root.style.removeProperty(n);
    applied = [];
    const set = (n, v) => { root.style.setProperty(n, v); applied.push(n); };
    set("--bg", p.bg);
    set("--bg-fill", p.bg2 ? `linear-gradient(135deg, ${p.bg}, ${p.bg2})` : p.bg);
    set("--panel", t.glass ? `color-mix(in srgb, ${p.surface} ${mode === "dark" ? 55 : 60}%, transparent)` : p.surface);
    set("--panel-solid", p.surface);
    set("--blur", t.glass ? "14px" : "0px");
    set("--text", p.text);
    set("--muted", p.muted);
    set("--line", p.border);
    set("--accent", p.accent);
    set("--accent-ink", luminance(p.accent) > 0.4 ? "#111111" : "#ffffff");
    set("--accent-soft", `color-mix(in srgb, ${p.accent} ${mode === "dark" ? 22 : 14}%, ${p.surface})`);
    for (const [k, v] of Object.entries(STATUS[mode])) set("--" + k, v);
    const r = Math.max(0, Math.min(24, Number(look.radius) || 0));
    set("--radius", r + "px");
    set("--radius-sm", Math.round(r * 0.75) + "px");
    set("--bw", (t.border || 1) + "px");
    const d = DENSITY[look.density] || DENSITY.comfortable;
    set("--pad", d.pad);
    set("--gap", d.gap);
    set("--font", FONTS[look.font || t.font || "system"] || FONTS.system);
    set("--shadow", look.shadow === "soft" ? (mode === "dark" ? "0 1px 2px rgb(0 0 0 / .45), 0 2px 8px rgb(0 0 0 / .25)"
      : "0 1px 2px rgb(0 0 0 / .06), 0 2px 8px rgb(0 0 0 / .05)")
      : look.shadow === "strong" ? "0 4px 14px rgb(0 0 0 / .2), 0 1px 3px rgb(0 0 0 / .14)" : "none");
    set("--maxw", WIDTH[look.width] || WIDTH.wide);
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", p.surface);
    if (!opts || opts.cache !== false) store.set("ad-look", JSON.stringify(look));
    window.dispatchEvent(new Event("ad-theme"));
  }

  const mode = {
    pref: modePref,
    effective,
    set(v) { if (isMode(v)) { store.set("ad-mode", v === look.mode ? null : v); apply(look); } },
    toggle() { mode.set(effective() === "dark" ? "light" : "dark"); },
  };
  if (mq && mq.addEventListener) mq.addEventListener("change", () => { if (modePref() === "auto") apply(look); });

  window.Theme = { THEMES, FONTS, DEFAULTS, apply, mode, get look() { return { ...look }; } };

  let cached = null;
  try { cached = JSON.parse(store.get("ad-look") || "null"); } catch (e) { /* ignore */ }
  apply(cached && typeof cached === "object" ? cached : DEFAULTS, { cache: false });
})();
