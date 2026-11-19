import { el } from "/static/core/api.js";

// One toast at a time. `action` ({label, onClick}) adds a button, e.g. "Batalkan" after a bulk
// action; such a toast stays longer so there is time to reach it.
let toastTimer = null;
export function toast(text, { kind = "info", action = null } = {}) {
  document.querySelectorAll(".toast").forEach((t) => t.remove());
  const node = el("div", { class: `toast toast-${kind}`, role: kind === "error" ? "alert" : "status" }, text);
  if (action) {
    node.append(el("button", { type: "button", class: "btn btn-ghost toast-action", onclick: () => {
      clearTimeout(toastTimer);
      node.remove();
      action.onClick();
    } }, action.label));
  }
  document.body.append(node);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.remove(), action ? 8000 : kind === "error" ? 6000 : 3500);
}
