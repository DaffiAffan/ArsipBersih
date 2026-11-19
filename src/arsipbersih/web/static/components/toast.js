import { el } from "/static/core/api.js";

let toastTimer = null;
export function toast(text, { kind = "info" } = {}) {
  document.querySelectorAll(".toast").forEach((t) => t.remove());
  const node = el("div", { class: `toast toast-${kind}`, role: kind === "error" ? "alert" : "status" }, text);
  document.body.append(node);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.remove(), kind === "error" ? 6000 : 3500);
}
