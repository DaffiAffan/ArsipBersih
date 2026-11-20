import { el } from "/static/core/api.js";

// One toast at a time. `action` ({label, onClick}) adds a button, e.g. "Batalkan" after a bulk
// action; such a toast stays longer so there is time to reach it.
let toastTimer = null;
// Two persistent live regions (polite for info, assertive for errors): screen readers announce a
// toast reliably only when it lands in a region that already existed before the text did. The
// toast itself carries no role, so each message is announced once.
function host(kind) {
  const id = kind === "error" ? "toast-host-alert" : "toast-host";
  let h = document.getElementById(id);
  if (!h) { h = el("div", { id, "aria-live": kind === "error" ? "assertive" : "polite" }); document.body.append(h); }
  return h;
}
export function toast(text, { kind = "info", action = null } = {}) {
  document.querySelectorAll(".toast").forEach((t) => t.remove());
  const node = el("div", { class: `toast toast-${kind}` }, text);
  if (action) {
    node.append(el("button", { type: "button", class: "btn btn-ghost toast-action", onclick: () => {
      clearTimeout(toastTimer);
      node.remove();
      action.onClick();
    } }, action.label));
  }
  host(kind).append(node);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.remove(), action ? 8000 : kind === "error" ? 6000 : 3500);
}
