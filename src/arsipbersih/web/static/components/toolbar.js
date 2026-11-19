// Thin one-row toolbar above a review grid: title, count, filter segments, sort, thumbnail size,
// "Nama file" (N), "Info" (I), the category explanation (ⓘ) and a "⋯" menu.
import { el } from "/static/core/api.js";
import { attachPopover } from "/static/components/popover.js";
import { attachMenu } from "/static/components/menu.js";

function pressed(btn, on) { btn.setAttribute("aria-pressed", String(Boolean(on))); }

// sort: {options: Array<{value, label}>, value}; size: {value, max}; help: {content, storageKey}
// (ⓘ popover, opened once per storageKey); menuItems: see attachMenu.
export function mountToolbar(container, { title, countText = "", segments = [], active, onSegment,
  sort = null, onSort, size = null, onSize, captions = false, onCaptions, onInfo, help = null, menuItems = [] }) {
  const count = el("span", { class: "toolbar-meta" }, countText);
  const segBtns = segments.map((s) => el("button", {
    type: "button", "data-value": s.value, onclick: () => { if (s.value !== current) { setActive(s.value); onSegment(s.value); } },
  }, s.label));
  let current = active;

  const parts = [el("h1", { class: "toolbar-title" }, title), count];
  if (segBtns.length) parts.push(el("div", { class: "segment", role: "group", "aria-label": "Tampilkan" }, ...segBtns));
  parts.push(el("span", { class: "toolbar-spacer" }));

  let sortSel = null;
  if (sort) {
    sortSel = el("select", { class: "toolbar-select", "aria-label": "Urutan", onchange: (e) => onSort(e.target.value) },
      ...sort.options.map((o) => el("option", { value: o.value }, o.label)));
    sortSel.value = sort.value;
    parts.push(sortSel);
  }
  let sizeInput = null;
  if (size) {
    sizeInput = el("input", { type: "range", min: "0", max: String(size.max), step: "1", "aria-label": "Ukuran thumbnail",
      oninput: (e) => onSize(Number(e.target.value)) });
    sizeInput.value = String(size.value);
    parts.push(el("label", { class: "toolbar-size", title: "Ukuran thumbnail" }, sizeInput));
  }
  const capBtn = onCaptions ? el("button", { type: "button", class: "btn btn-ghost", title: "Nama file (N)", onclick: () => onCaptions() },
    "Nama file") : null;
  const infoBtn = onInfo ? el("button", { type: "button", class: "btn btn-ghost", title: "Panel info (I)", onclick: () => onInfo() },
    "Info") : null;
  const helpBtn = help ? el("button", { type: "button", class: "btn btn-ghost btn-icon", "aria-label": "Tentang kategori ini", title: "Tentang kategori ini" }, "ⓘ") : null;
  const moreBtn = menuItems.length ? el("button", { type: "button", class: "btn btn-ghost btn-icon", "aria-label": "Aksi lain", title: "Aksi lain" }, "⋯") : null;
  parts.push(capBtn, infoBtn, helpBtn, moreBtn);

  const bar = el("div", { class: "toolbar" }, ...parts);
  container.append(bar);
  if (helpBtn) attachPopover(helpBtn, help.content, { storageKey: help.storageKey, openOnce: Boolean(help.storageKey) });
  if (moreBtn) attachMenu(moreBtn, menuItems);

  function setActive(v) {
    current = v;
    for (const b of segBtns) pressed(b, b.dataset.value === v);
  }
  setActive(active);
  if (capBtn) pressed(capBtn, captions);

  return {
    el: bar,
    setCount(text) { count.textContent = text; },
    setActive,
    setCaptions(on) { if (capBtn) pressed(capBtn, on); },
    setInfo(on) { if (infoBtn) pressed(infoBtn, on); },
    setSize(v) { if (sizeInput) sizeInput.value = String(v); },
  };
}
