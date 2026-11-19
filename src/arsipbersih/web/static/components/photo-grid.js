// Dense review grid: loads a tab snapshot (`order`) page by page, draws thumb cells, keeps the
// keyboard focus and releases images far from the screen (U1, memory on 8 GB RAM).
import { api, el } from "/static/core/api.js";
import { CATEGORY_LABELS } from "/static/components/labels.js";
import { renderThumb, updateThumb } from "/static/components/thumb.js";

const PAGE = 120;

function fmtScore(v) {
  const n = Number(v);
  return (Math.abs(n) >= 10 ? n.toFixed(1) : n.toFixed(2)).replace(".", ",");
}

// onActivate(item): single click; onOpen(index): double click; onFocus(item): focus moved;
// onAppend(fresh): new items loaded; onError(err): request failed.
export function createPhotoGrid({ category, scroller, emptyText, onActivate, onOpen, onFocus, onAppend, onError }) {
  const gridEl = el("div", { class: "photo-grid" });
  const sentinel = el("div", { class: "grid-more" });
  const root = el("div", { class: "grid-col" }, gridEl, sentinel);
  let order = []; // unit ids of the current tab snapshot
  let next = 0; // index into `order` of the next id to fetch
  let items = [];
  const byId = new Map(); // unit_id -> item
  const cells = new Map(); // unit_id -> element
  let focus = 0;
  let captions = false;
  let loading = false;
  let loadFailed = false;
  let generation = 0;
  let view = "all";

  function indexOf(id) { const it = byId.get(id); return it ? it.idx : -1; }

  function repaint(item) {
    const cell = cells.get(item.unit_id);
    if (cell) updateThumb(cell, item, { caption: captions, focused: item.idx === focus });
  }

  // Placeholder detection: /media/thumb serves its placeholder SVG with a 1x1 intrinsic size,
  // so a loaded image with naturalWidth/Height <= 1 is the placeholder. A failed load counts as
  // "no preview" too. Only a real, loaded thumbnail makes the cell a target of the bulk discard.
  function watchThumb(cell, img, item) {
    item.thumbState = "loading";
    const noPreview = () => {
      item.thumbState = "none";
      cell.classList.add("is-noprev");
      cell.querySelector(".thumb-noprev").hidden = false;
    };
    img.addEventListener("load", () => {
      if (!img.getAttribute("src")) return; // released meanwhile
      if (img.naturalWidth <= 1 && img.naturalHeight <= 1) { noPreview(); return; }
      item.thumbState = "ok";
    });
    img.addEventListener("error", () => { if (img.getAttribute("src")) noPreview(); });
    if (!item.thumb) noPreview();
  }

  // Cells more than ~2 screens from the visible area drop their image and reload it when they
  // come back. The cell keeps its size (aspect-ratio), so scrolling never jumps. A released cell
  // is not "thumbnail shown", so the bulk discard never targets it.
  function releaseThumb(item, cell) {
    const img = cell.querySelector("img");
    if (!item.thumb || item.thumbState === "none" || !img.hasAttribute("src")) return;
    img.removeAttribute("src");
    item.thumbState = "released";
  }

  function restoreThumb(item, cell) {
    const img = cell.querySelector("img");
    if (!item.thumb || item.thumbState !== "released") return;
    item.thumbState = "loading";
    img.setAttribute("src", item.thumb);
  }

  const thumbObserver = typeof IntersectionObserver !== "undefined"
    ? new IntersectionObserver((entries) => {
      for (const e of entries) {
        const item = byId.get(e.target.dataset.id);
        if (!item) continue;
        if (e.isIntersecting) restoreThumb(item, e.target); else releaseThumb(item, e.target);
      }
    }, { root: scroller, rootMargin: "200% 0px" })
    : null;

  function makeCell(item) {
    const extra = (item.badges || []).map((c) => CATEGORY_LABELS[c] || c);
    const why = [item.reason, item.score != null ? fmtScore(item.score) : null, ...extra].filter(Boolean).join(" · ");
    const cell = renderThumb(item, { caption: captions });
    if (why) cell.title = why;
    let clickTimer = null; // delay single-click so a double-click does not toggle first
    cell.addEventListener("click", () => {
      clearTimeout(clickTimer);
      clickTimer = setTimeout(() => { setFocus(indexOf(item.unit_id), false); onActivate(item); }, 250);
    });
    cell.addEventListener("dblclick", () => { clearTimeout(clickTimer); onOpen(indexOf(item.unit_id)); });
    cells.set(item.unit_id, cell);
    watchThumb(cell, cell.querySelector("img"), item);
    if (thumbObserver) thumbObserver.observe(cell);
    return cell;
  }

  function columns() {
    return Math.max(1, getComputedStyle(gridEl).gridTemplateColumns.split(" ").filter(Boolean).length);
  }

  function setFocus(i, scroll = true) {
    if (i < 0 || i >= items.length) return;
    const prev = items[focus];
    focus = i;
    if (prev) repaint(prev);
    repaint(items[focus]);
    const cell = cells.get(items[focus].unit_id);
    if (scroll && cell && cell.scrollIntoView) cell.scrollIntoView({ block: "nearest" });
    if (i >= items.length - 24) loadMore();
    onFocus(items[focus]);
  }

  function showEmpty() { sentinel.replaceChildren(el("p", { class: "empty" }, emptyText(view))); }

  function showRetry(err) {
    onError(err);
    sentinel.replaceChildren(el("button", { type: "button", class: "btn btn-secondary", onclick: () => {
      loadFailed = false;
      sentinel.textContent = "Memuat…";
      loadMore();
    } }, "Coba lagi"));
  }

  async function loadMore() {
    if (loading || loadFailed || next >= order.length) return;
    loading = true;
    const gen = generation;
    try {
      const ids = order.slice(next, next + PAGE);
      const data = await api("POST", `/api/review/grid/${encodeURIComponent(category)}/items`, { unit_ids: ids });
      if (gen !== generation) return;
      next += ids.length;
      const base = items.length;
      const fresh = data.items.filter((it) => !byId.has(it.unit_id));
      items.push(...fresh);
      fresh.forEach((it, n) => { it.idx = base + n; byId.set(it.unit_id, it); gridEl.append(makeCell(it)); });
      onAppend(fresh);
      sentinel.textContent = next < order.length ? "Memuat…" : "";
      if (!items.length && next >= order.length) showEmpty();
    } catch (e) {
      if (gen === generation) { loadFailed = true; showRetry(e); }
    } finally { if (gen === generation) loading = false; }
    // keep filling while the sentinel is still near the screen (the observer will not re-fire)
    if (gen === generation && next < order.length && !loadFailed
        && sentinel.getBoundingClientRect().top < window.innerHeight + 600) loadMore();
  }

  // Takes a fresh snapshot of the tab and draws its first page.
  async function reload({ sort, view: v }) {
    generation += 1;
    const gen = generation;
    view = v;
    if (thumbObserver) thumbObserver.disconnect();
    cells.clear();
    byId.clear();
    items = []; order = []; next = 0; focus = 0;
    loading = false; loadFailed = false;
    gridEl.replaceChildren();
    sentinel.textContent = "Memuat…";
    try {
      const snap = await api("GET", `/api/review/grid/${encodeURIComponent(category)}/order?sort=${sort}&view=${v}`);
      if (gen !== generation) return false;
      order = snap.unit_ids;
    } catch (e) {
      if (gen === generation) { loadFailed = true; showRetry(e); }
      return false;
    }
    await loadMore();
    if (gen !== generation) return false;
    if (items.length) setFocus(0, false); else onFocus(null);
    return true;
  }

  // Drops a unit that left this category; returns its old index (-1 when unknown).
  function remove(unitId) {
    const i = indexOf(unitId);
    if (i < 0) return -1;
    const cell = cells.get(unitId);
    if (cell) { if (thumbObserver) thumbObserver.unobserve(cell); cell.remove(); }
    cells.delete(unitId);
    byId.delete(unitId);
    items.splice(i, 1);
    items.forEach((it, n) => { it.idx = n; });
    const oi = order.indexOf(unitId);
    if (oi >= 0) { order.splice(oi, 1); if (oi < next) next -= 1; }
    if (!items.length) {
      focus = 0;
      if (next >= order.length) showEmpty();
      onFocus(null);
      loadMore();
    } else {
      if (focus > i) focus -= 1;
      setFocus(Math.min(focus, items.length - 1), false);
    }
    return i;
  }

  // Undecided units whose real thumbnail is loaded and at least half inside `band` ({top, bottom}).
  function onScreen(band) {
    const out = [];
    for (const it of items) {
      if (it.decision || it.thumbState !== "ok") continue;
      const cell = cells.get(it.unit_id);
      if (!cell) continue;
      const r = cell.getBoundingClientRect();
      if (r.height <= 0) continue;
      const shown = Math.min(r.bottom, band.bottom) - Math.max(r.top, band.top);
      if (shown >= r.height / 2) out.push(it);
    }
    return out;
  }

  if (typeof IntersectionObserver !== "undefined") {
    new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) loadMore();
    }, { root: scroller, rootMargin: "600px" }).observe(sentinel);
  }

  return {
    el: root,
    get items() { return items; },
    get focus() { return focus; },
    get total() { return order.length; },
    get done() { return next >= order.length; },
    focused: () => items[focus],
    byId: (id) => byId.get(id),
    cell: (id) => cells.get(id),
    indexOf, repaint, setFocus, columns, loadMore, reload, remove, onScreen,
    setSize(px) { gridEl.style.setProperty("--cell-min", `${px}px`); },
    setCaptions(on) { captions = on; for (const it of items) repaint(it); },
  };
}
