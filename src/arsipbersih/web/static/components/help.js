import { el } from "/static/core/api.js";
import { bindKeys } from "/static/core/keys.js";

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
