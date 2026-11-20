// Small floating panels under a button: the category explanation (ⓘ) and the "⋯" menu.
// Esc or a click outside closes them; Esc puts focus back on the button.
import { load, save } from "/static/core/storage.js";

let uid = 0;

// Shared open/close/position logic. `node` is placed below `button`, right-aligned to it when
// there is no room on the right. `onOpen`/`onClose` let callers add keys or focus handling.
export function floating(button, node, { onOpen = null, onClose = null } = {}) {
  node.id = node.id || `float-${++uid}`;
  node.hidden = true;
  button.setAttribute("aria-controls", node.id);
  button.setAttribute("aria-expanded", "false");
  document.body.append(node);

  function place() {
    const r = button.getBoundingClientRect();
    node.style.position = "fixed";
    node.style.top = `${Math.round(r.bottom + 6)}px`;
    const w = node.offsetWidth;
    const left = Math.min(r.left, window.innerWidth - w - 16);
    node.style.left = `${Math.max(16, Math.round(left))}px`;
  }

  function onDocDown(ev) {
    if (!node.contains(ev.target) && !button.contains(ev.target)) close();
  }
  function onDocKey(ev) {
    if (ev.key !== "Escape") return;
    ev.preventDefault();
    close();
    button.focus();
  }

  function open() {
    if (!node.hidden) return;
    node.hidden = false;
    button.setAttribute("aria-expanded", "true");
    place();
    document.addEventListener("pointerdown", onDocDown, true);
    document.addEventListener("keydown", onDocKey, true);
    window.addEventListener("resize", place);
    if (onOpen) onOpen();
  }

  function close() {
    if (node.hidden) return;
    node.hidden = true;
    button.setAttribute("aria-expanded", "false");
    document.removeEventListener("pointerdown", onDocDown, true);
    document.removeEventListener("keydown", onDocKey, true);
    window.removeEventListener("resize", place);
    if (onClose) onClose();
  }

  button.addEventListener("click", () => { if (node.hidden) open(); else close(); });
  return { open, close, get isOpen() { return !node.hidden; } };
}

// Explanation popover. With `openOnce` + `storageKey` it opens by itself the first time
// (e.g. the first visit to a category) and remembers that in localStorage.
export function attachPopover(button, content, { storageKey = null, openOnce = false } = {}) {
  const node = document.createElement("div");
  node.className = "popover";
  node.setAttribute("role", "dialog");
  node.setAttribute("aria-label", button.getAttribute("aria-label") || "Penjelasan");
  node.append(content);
  const f = floating(button, node);
  if (openOnce && storageKey && !load("local", storageKey)) {
    save("local", storageKey, true);
    requestAnimationFrame(() => f.open());
  }
  return f;
}
