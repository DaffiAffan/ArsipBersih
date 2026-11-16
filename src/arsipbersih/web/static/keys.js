// Peta tombol bertumpuk: binding terakhir menang; unbind memulihkan yang di bawahnya.
const stack = [];
let listening = false;

function isTyping(target) {
  if (!target || !target.tagName) return false;
  const tag = target.tagName.toLowerCase();
  return tag === "input" || tag === "textarea" || tag === "select" || target.isContentEditable === true;
}

function normalize(key) {
  return key.length === 1 ? key.toLowerCase() : key;
}

function onKeyDown(ev) {
  if (ev.ctrlKey || ev.metaKey || ev.altKey) return;
  if (isTyping(ev.target)) return;
  const key = normalize(ev.key);
  const top = stack[stack.length - 1];
  if (!top || !Object.prototype.hasOwnProperty.call(top, key)) return;
  ev.preventDefault();
  top[key](ev);
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
