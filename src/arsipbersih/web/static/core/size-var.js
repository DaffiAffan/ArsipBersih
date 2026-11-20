// Mirrors an element's rendered height into a CSS custom property on another element, so sticky
// offsets follow a toolbar that wraps to two rows at narrow widths instead of a magic number.
export function exposeHeight(node, prop, target = node.parentElement) {
  if (!node || !target || typeof ResizeObserver === "undefined") return;
  const ro = new ResizeObserver(() => target.style.setProperty(prop, `${node.offsetHeight}px`));
  ro.observe(node);
}
