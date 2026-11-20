import { el } from "/static/core/api.js";

// steps: Array<{title, text, state: "done"|"current"|"later", action?: HTMLElement, meter?: number (0-100)}>
export function renderSteps(container, steps) {
  container.replaceChildren(...steps.map((s) => el("li", {
    class: `is-${s.state}`, "aria-current": s.state === "current" ? "step" : undefined,
  },
    el("div", {},
      el("h2", {}, s.title),
      el("p", {}, s.text),
      s.meter == null ? null : el("span", { class: "progress step-meter", role: "progressbar", "aria-label": "Kemajuan langkah", "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": s.meter },
        el("i", { style: `width:${s.meter}%` })),
      s.action || null))));
}
