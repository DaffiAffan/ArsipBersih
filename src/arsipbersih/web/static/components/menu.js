// "⋯" menu: a short list of actions under its button. ↑↓ move between items, Esc closes.
// Page shortcuts stay inert while it is open (arrows must not move the grid focus).
import { el } from "/static/core/api.js";
import { bindKeys } from "/static/core/keys.js";
import { floating } from "/static/components/popover.js";

const SVG = "http://www.w3.org/2000/svg";
// Inline "⋯" icon for menu buttons (the text glyph renders tiny in Segoe UI). Decorative: the
// button carries the accessible name.
export function moreIcon() {
  const svg = document.createElementNS(SVG, "svg");
  for (const [k, v] of Object.entries({ width: "18", height: "18", viewBox: "0 0 18 18", "aria-hidden": "true", fill: "currentColor" })) svg.setAttribute(k, v);
  for (const cx of [4, 9, 14]) {
    const c = document.createElementNS(SVG, "circle");
    c.setAttribute("cx", String(cx)); c.setAttribute("cy", "9"); c.setAttribute("r", "1.6");
    svg.append(c);
  }
  return svg;
}

// items: Array<{label, kbd?, onClick, disabled?, title?, danger?}>
export function attachMenu(button, items) {
  button.setAttribute("aria-haspopup", "menu");
  let unbind = null;
  const buttons = items.map((it) => el("button", {
    type: "button", role: "menuitem", class: `btn btn-ghost${it.danger ? " is-danger" : ""}`,
    title: it.title || null, "aria-disabled": it.disabled ? "true" : null,
    onclick: () => { f.close(); button.focus(); if (!it.disabled) it.onClick(); },
  }, it.label, it.kbd ? el("kbd", { class: "kbd" }, it.kbd) : null));
  const node = el("div", { class: "menu", role: "menu", "aria-label": button.getAttribute("aria-label") || "Menu" }, ...buttons);
  node.addEventListener("keydown", (ev) => {
    if (ev.key !== "ArrowDown" && ev.key !== "ArrowUp") return;
    ev.preventDefault();
    const at = buttons.indexOf(document.activeElement);
    const step = ev.key === "ArrowDown" ? 1 : -1;
    buttons[(at + step + buttons.length) % buttons.length].focus();
  });
  const f = floating(button, node, {
    onOpen: () => { unbind = bindKeys({}); if (buttons[0]) buttons[0].focus(); },
    onClose: () => { if (unbind) unbind(); unbind = null; },
  });
  return f;
}
