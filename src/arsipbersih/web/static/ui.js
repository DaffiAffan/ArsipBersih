import { el, fmtBytes } from "/static/api.js";

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

// Resolves true when the owner confirms. <dialog>.showModal() traps focus and Esc cancels.
export function confirmDialog({ title, body, okLabel, danger = false, typeToConfirm = null }) {
  return new Promise((resolve) => {
    const dlg = el("dialog", { class: "dialog", "aria-label": title });
    const ok = el("button", { type: "button", class: danger ? "btn btn-danger" : "btn btn-primary" }, okLabel);
    const cancel = el("button", { type: "button", class: "btn btn-secondary" }, "Batal");
    let input = null;
    if (typeToConfirm) {
      input = el("input", { type: "text", class: "input", autocomplete: "off", "aria-label": `Ketik ${typeToConfirm}` });
      ok.disabled = true;
      input.addEventListener("input", () => { ok.disabled = input.value !== typeToConfirm; });
    }
    const done = (v) => { dlg.close(); dlg.remove(); resolve(v); };
    ok.addEventListener("click", () => { if (!ok.disabled) done(true); });
    cancel.addEventListener("click", () => done(false));
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); done(false); });
    dlg.append(
      el("h3", {}, title),
      ...[].concat(body || []).map((t) => el("p", {}, t)),
      ...(input ? [input] : []),
      el("div", { class: "dialog-actions" }, cancel, ok),
    );
    document.body.append(dlg);
    dlg.showModal();
    (input || cancel).focus();
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
let helpOpen = false;

function showHelp() {
  if (helpOpen || !helpSections) return;
  helpOpen = true;
  const dlg = el("dialog", { class: "dialog help-dialog", "aria-label": "Pintasan keyboard" });
  const close = el("button", { type: "button", class: "btn btn-secondary" }, "Tutup");
  const done = () => { dlg.close(); dlg.remove(); helpOpen = false; };
  close.addEventListener("click", done);
  dlg.addEventListener("cancel", (e) => { e.preventDefault(); done(); });
  dlg.append(
    el("h3", {}, "Pintasan keyboard"),
    ...helpSections.map((s) => el("section", { class: "help-section" },
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
