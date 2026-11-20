import { el } from "/static/core/api.js";
import { bindKeys } from "/static/core/keys.js";

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
