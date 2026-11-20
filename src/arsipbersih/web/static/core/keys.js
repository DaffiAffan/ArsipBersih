// Peta tombol bertumpuk: binding terakhir menang; unbind memulihkan yang di bawahnya.
const stack = [];
let listening = false;

function isTyping(target) {
  if (!target || !target.tagName) return false;
  const tag = target.tagName.toLowerCase();
  return tag === "input" || tag === "textarea" || tag === "select" || target.isContentEditable === true;
}

function isInteractive(target) {
  if (!target || !target.tagName) return false;
  const tag = target.tagName.toLowerCase();
  return tag === "button" || tag === "a" || tag === "video" || tag === "audio" || target.getAttribute("role") === "button";
}

const NO_REPEAT = new Set(["s", "d", "Enter"]);
const ACTIVATES = new Set(["Enter", " "]);

function normalize(key) {
  return key.length === 1 ? key.toLowerCase() : key;
}

function onKeyDown(ev) {
  if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
  if (isTyping(ev.target)) return;
  const key = normalize(ev.key);
  if (ACTIVATES.has(key) && isInteractive(ev.target)) return;
  if (ev.repeat && NO_REPEAT.has(key)) {
    const t = stack[stack.length - 1];
    if (t && Object.prototype.hasOwnProperty.call(t, key)) ev.preventDefault();
    return;
  }
  const top = stack[stack.length - 1];
  if (!top || !Object.prototype.hasOwnProperty.call(top, key)) return;
  ev.preventDefault();
  top[key](ev);
}

// Click handler for a button that must not keep focus after a mouse click: onKeyDown ignores
// Enter/Space on a focused button, so a clicked button would swallow those shortcuts (and Space
// would click it again). Keyboard activation (detail === 0) keeps focus for Tab users.
export function onClick(fn) {
  return (ev) => {
    if (ev && ev.detail > 0 && ev.currentTarget && ev.currentTarget.blur) ev.currentTarget.blur();
    return fn(ev);
  };
}

export function bindKeys(map) {
  const norm = {};
  for (const [k, fn] of Object.entries(map)) norm[normalize(k)] = fn;
  stack.push(norm);
  if (!listening) {
    document.addEventListener("keydown", onKeyDown);
    listening = true;
  }
  return function unbind() {
    const i = stack.indexOf(norm);
    if (i >= 0) stack.splice(i, 1);
    if (!stack.length && listening) {
      document.removeEventListener("keydown", onKeyDown);
      listening = false;
    }
  };
}
