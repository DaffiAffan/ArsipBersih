import { el, fmtBytes } from "/static/api.js";
import { bindKeys } from "/static/keys.js";

export { fmtBytes };

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"];

export function fmtNum(n) {
  return new Intl.NumberFormat("id-ID").format(Number(n) || 0);
}

function hm(d) {
  return d.toLocaleTimeString("id-ID", { hour: "2-digit", minute: "2-digit", hour12: false }).replace(":", ".");
}

// "baru saja", "N menit lalu", "hari ini, 14.05", "kemarin, 22.12", "16 Nov 2026, 15.45".
export function fmtTime(iso, now = new Date()) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const diff = now.getTime() - d.getTime();
  if (diff >= 0 && diff < 60000) return "baru saja";
  if (diff >= 0 && diff < 3600000) return `${Math.floor(diff / 60000)} menit lalu`;
  const startOf = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((startOf(now) - startOf(d)) / 86400000);
  if (days === 0) return `hari ini, ${hm(d)}`;
  if (days === 1) return `kemarin, ${hm(d)}`;
  return `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}, ${hm(d)}`;
}

// Shared modal on <dialog> (showModal traps focus, Esc cancels). Resolves `value()` on OK and
// `cancelValue` on cancel/Esc. `nodes` go between the title and the buttons; `onReady(ok)` lets the
// caller wire the OK button (e.g. disable it); `submitOnEnter` is a field where Enter confirms.
export function modalDialog({ title, nodes = [], okLabel, okClass = "btn btn-primary", value, cancelValue,
  onReady = null, submitOnEnter = null, focus = null }) {
  return new Promise((resolve) => {
    const dlg = el("dialog", { class: "dialog", "aria-label": title });
    const ok = el("button", { type: "button", class: okClass }, okLabel);
    const cancel = el("button", { type: "button", class: "btn btn-secondary" }, "Batal");
    if (onReady) onReady(ok);
    // Empty key layer: page shortcuts (S/D/arrows…) stay inert under the dialog; the dialog
    // itself still gets Esc (cancel), Enter on its buttons and typing.
    const unbindKeys = bindKeys({});
    // Removed on its own "close" event, so the event still reaches document listeners.
    dlg.addEventListener("close", () => dlg.remove());
    const done = (v) => { unbindKeys(); dlg.close(); resolve(v); };
    const submit = () => { if (!ok.disabled) done(value()); };
    ok.addEventListener("click", submit);
    cancel.addEventListener("click", () => done(cancelValue));
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); done(cancelValue); });
    if (submitOnEnter) submitOnEnter.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); submit(); } });
    dlg.append(el("h3", {}, title), ...nodes, el("div", { class: "dialog-actions" }, cancel, ok));
    document.body.append(dlg);
    dlg.showModal();
    (focus || cancel).focus();
  });
}

// Resolves true when the owner confirms.
export function confirmDialog({ title, body, okLabel, danger = false, typeToConfirm = null }) {
  let input = null;
  if (typeToConfirm) input = el("input", { type: "text", class: "input", autocomplete: "off", "aria-label": `Ketik ${typeToConfirm}` });
  return modalDialog({
    title, okLabel, okClass: danger ? "btn btn-danger" : "btn btn-primary",
    nodes: [...[].concat(body || []).map((t) => el("p", {}, t)), ...(input ? [input] : [])],
    value: () => true, cancelValue: false, focus: input,
    onReady: (ok) => {
      if (!input) return;
      ok.disabled = true;
      input.addEventListener("input", () => { ok.disabled = input.value !== typeToConfirm; });
    },
  });
}

let toastTimer = null;
export function toast(text, { kind = "info" } = {}) {
  document.querySelectorAll(".toast").forEach((t) => t.remove());
  const node = el("div", { class: `toast toast-${kind}`, role: kind === "error" ? "alert" : "status" }, text);
  document.body.append(node);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.remove(), kind === "error" ? 6000 : 3500);
}

// --- keyboard help ("?") ----------------------------------------------------------------

let helpSections = null;
const extraHelp = []; // sections added by overlays (e.g. the fullscreen viewer) while they are open
let helpOpen = false;

function showHelp() {
  if (helpOpen || !helpSections) return;
  helpOpen = true;
  const dlg = el("dialog", { class: "dialog help-dialog", "aria-label": "Pintasan keyboard" });
  const close = el("button", { type: "button", class: "btn btn-secondary" }, "Tutup");
  const unbindKeys = bindKeys({}); // page shortcuts stay inert while help is open
  dlg.addEventListener("close", () => dlg.remove()); // after document listeners saw "close"
  const done = () => { unbindKeys(); dlg.close(); helpOpen = false; };
  close.addEventListener("click", done);
  dlg.addEventListener("cancel", (e) => { e.preventDefault(); done(); });
  dlg.append(
    el("h3", {}, "Pintasan keyboard"),
    ...[...extraHelp, ...helpSections].map((s) => el("section", { class: "help-section" },
      el("h4", {}, s.title),
      el("dl", { class: "help-list" }, ...s.keys.flatMap(([key, label]) => [
        el("dt", {}, el("kbd", { class: "kbd" }, key)),
        el("dd", {}, label),
      ])),
    )),
    el("div", { class: "dialog-actions" }, close),
  );
  document.body.append(dlg);
  dlg.showModal();
  close.focus();
}

function onHelpKey(ev) {
  if (ev.key !== "?" || ev.ctrlKey || ev.metaKey || ev.altKey) return;
  const t = ev.target;
  const tag = t && t.tagName ? t.tagName.toLowerCase() : "";
  if (tag === "input" || tag === "textarea" || tag === "select" || (t && t.isContentEditable)) return;
  if (document.querySelector("dialog[open]")) return;
  ev.preventDefault();
  showHelp();
}

export function registerHelp(sections) {
  if (!helpSections) document.addEventListener("keydown", onHelpKey);
  helpSections = sections;
}

// Adds help sections while an overlay is open; returns a function that removes them again.
export function pushHelp(sections) {
  extraHelp.unshift(...sections);
  return () => { for (const s of sections) { const n = extraHelp.indexOf(s); if (n >= 0) extraHelp.splice(n, 1); } };
}
