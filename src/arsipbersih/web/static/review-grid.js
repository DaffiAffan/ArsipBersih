// Review grid: tabs, density, inspector, floating Apply bar and dwell-based "seen" (D-032).
import { api, el } from "/static/api.js";
import { bindKeys } from "/static/keys.js";
import { openViewer, CATEGORY_LABELS } from "/static/viewer.js";
import { reviewNav } from "/static/nav.js";
import { fmtNum, registerHelp, toast } from "/static/ui.js";
import { createSeenQueue, createSeenTracker } from "/static/seen-dwell.js";
import { createInspectorLoader } from "/static/inspector.js";
import { mountFloatBar } from "/static/floatbar.js";

const main = document.getElementById("rv-main");
const root = document.getElementById("grid-root");
const scroller = document.querySelector(".content");
const category = main.dataset.category;
const label = main.dataset.label || category;

const PAGE = 120;
const SEEN_CHUNK = 1000;
const SEEN_DELAY_MS = 1000;
const SIZES = [100, 150, 220]; // slider value -> minimum column width in px (0 = cepat, 2 = santai)
const VIEWS = [["all", "Semua"], ["unseen", "Belum dilihat"], ["keep", "Keep"]];
const EMPTY = { all: "Tidak ada item di kategori ini.", unseen: "Semua foto di sini sudah dilihat.", keep: "Belum ada yang di-Keep." };
const PREF_KEY = `ab.grid.${category}`;

// --- preferences (view only; per category) -------------------------------------------

function loadPrefs() {
  const prefs = { size: 1, inspector: false };
  try {
    const raw = JSON.parse(localStorage.getItem(PREF_KEY) || "null");
    if (raw && SIZES[raw.size] !== undefined) prefs.size = raw.size;
    if (raw && typeof raw.inspector === "boolean") prefs.inspector = raw.inspector;
  } catch (_) { /* storage blocked or corrupt: defaults */ }
  return prefs;
}

function savePrefs() {
  try { localStorage.setItem(PREF_KEY, JSON.stringify(prefs)); } catch (_) { /* best effort */ }
}

const prefs = loadPrefs();

// --- state ---------------------------------------------------------------------------

let view = "all";
let sort = "score";
let order = []; // unit ids of the current tab snapshot
let next = 0; // index into `order` of the next id to fetch
let items = [];
const byId = new Map(); // unit_id -> item
const cells = new Map(); // unit_id -> element
let focus = 0;
let busy = false;
let loading = false;
let loadFailed = false;
let generation = 0;
let viewerState = null; // { vitems, loaded:Set, handle }

function fail(err) { toast(err && err.message ? err.message : String(err), { kind: "error" }); }
function indexOfUnit(id) { const it = byId.get(id); return it ? it.idx : -1; }
function focusedItem() { return items[focus]; }

// --- seen tracking ---------------------------------------------------------------------

const localSeen = new Set(); // queued or sent this page: drawn with the dot right away
let seenSent = false;
const seenQ = createSeenQueue({
  chunk: SEEN_CHUNK, delayMs: SEEN_DELAY_MS,
  onSent: (ids) => { seenSent = true; for (const id of ids) { const it = byId.get(id); if (it) it.seen = true; } },
});

function queueSeen(item) {
  if (!item || item.seen || !seenQ.add(item.unit_id)) return;
  localSeen.add(item.unit_id);
  const cell = cells.get(item.unit_id);
  if (cell) tracker.unobserve(cell);
  repaint(item);
}

// Grid-side actions (click, S/D/Enter) only count as "seen" when the owner actually saw the
// real thumbnail. A cell still loading or showing the placeholder becomes seen only by being
// opened in fullscreen (openFull/onIndex call queueSeen directly).
function queueSeenIfShown(item) {
  if (item && item.thumbState === "ok") queueSeen(item);
}

// Resolves true only when every queued id reached the server.
function flushSeen() {
  return seenQ.flush().then((ok) => {
    if (seenSent) { seenSent = false; bar.refresh(); reviewNav.refresh(); }
    return ok;
  });
}

// Cells under the sticky toolbar or the floating Apply bar are not "on screen" for dwell.
const tracker = createSeenTracker({
  root: scroller,
  onSeen: (id) => queueSeen(byId.get(id)),
  insets: () => {
    const fb = root.querySelector(".floatbar");
    const bottom = scroller && fb ? scroller.getBoundingClientRect().bottom - fb.getBoundingClientRect().top : 0;
    return { top: toolbarEl.offsetHeight, bottom: bottom + 8 };
  },
});

// Dwell only counts while the grid itself is what the owner looks at.
function cellReady(item) {
  return () => item.thumbState === "ok" && !viewerState && !document.querySelector("dialog[open]");
}

// --- shell -----------------------------------------------------------------------------

const countEl = el("span", { class: "grid-count muted" });
const tabs = VIEWS.map(([v, text]) => el("button", {
  type: "button", class: "pill", role: "tab", "data-view": v, onclick: () => { if (view !== v) { view = v; reload(); } },
}, text));
const sortSel = el("select", { class: "grid-select", "aria-label": "Urutan", onchange: (e) => { sort = e.target.value; reload(); } },
  el("option", { value: "score" }, "Skor (terburuk dulu)"),
  el("option", { value: "date" }, "Tanggal"));
const sizeInput = el("input", { type: "range", min: "0", max: String(SIZES.length - 1), step: "1", "aria-label": "Ukuran thumbnail",
  oninput: (e) => { prefs.size = Number(e.target.value); savePrefs(); applySize(); } });
const infoBtn = el("button", { type: "button", class: "btn btn-ghost", onclick: () => toggleInspector() },
  "Info ", el("kbd", { class: "kbd" }, "I"));
const gridEl = el("div", { class: "photo-grid" });
const sentinel = el("div", { class: "grid-more" });
const inspEl = el("aside", { class: "inspector grid-inspector", "aria-label": "Info foto" });

root.classList.add("grid-root");
const toolbarEl = el("div", { class: "grid-toolbar" },
    el("div", { class: "grid-tabs", role: "tablist", "aria-label": "Tampilan" }, ...tabs),
    countEl,
    el("span", { class: "grid-spacer" }),
    el("label", { class: "grid-ctl" }, "Urutan ", sortSel),
    el("label", { class: "grid-ctl" }, el("span", { "aria-hidden": "true" }, "▦"), sizeInput, el("span", { "aria-hidden": "true" }, "▢")),
    infoBtn);
root.replaceChildren(
  toolbarEl,
  el("p", { class: "grid-hint muted" },
    el("kbd", { class: "kbd" }, "S"), " keep · ", el("kbd", { class: "kbd" }, "D"), " delete · ",
    el("kbd", { class: "kbd" }, "Spasi"), " layar penuh · ", el("kbd", { class: "kbd" }, "?"), " bantuan"),
  el("div", { class: "grid-layout" }, el("div", { class: "grid-col" }, gridEl, sentinel), inspEl));
const bar = mountFloatBar(root, {
  category, mode: "grid", label,
  beforeApply: async () => {
    if (await flushSeen()) return true;
    toast("Tidak bisa mengirim daftar item yang sudah dilihat. Coba lagi sebentar lagi.", { kind: "error" });
    return false;
  },
  onApplied: () => { reload().catch(fail); },
});

function applySize() {
  sizeInput.value = String(prefs.size);
  gridEl.style.setProperty("--cell-min", `${SIZES[prefs.size]}px`);
  gridEl.classList.toggle("is-dense", prefs.size === 0);
}

function toggleInspector(open = !prefs.inspector) {
  prefs.inspector = open;
  savePrefs();
  inspEl.hidden = !open;
  infoBtn.setAttribute("aria-pressed", String(open));
  if (open) showInspector();
}

function paintToolbar() {
  for (const t of tabs) {
    const on = t.dataset.view === view;
    t.classList.toggle("is-active", on);
    t.setAttribute("aria-selected", String(on));
  }
  countEl.textContent = `${fmtNum(order.length)} item`;
}

// --- inspector ---------------------------------------------------------------------------

const insp = createInspectorLoader(inspEl, { onError: fail });

function showInspector() {
  if (!prefs.inspector) return;
  const it = focusedItem();
  insp.show(it ? it.unit_id : null, () => (it ? it.decision : null), (decision) => {
    queueSeenIfShown(it);
    decide(it.unit_id, decision);
  });
}

// --- cells -------------------------------------------------------------------------------

function fmtScore(v) {
  const n = Number(v);
  return (Math.abs(n) >= 10 ? n.toFixed(1) : n.toFixed(2)).replace(".", ",");
}

function paintCell(cell, item, i) {
  cell.classList.toggle("is-keep", item.decision === "keep");
  cell.classList.toggle("is-discard", item.decision === "discard");
  cell.classList.toggle("is-focused", i === focus);
  cell.querySelector(".seen-dot").hidden = !(item.seen || localSeen.has(item.unit_id));
  const mark = cell.querySelector(".cell-mark");
  mark.className = `cell-mark badge ${item.decision === "keep" ? "badge-keep" : "badge-danger"}`;
  mark.textContent = item.decision === "keep" ? "Keep" : item.decision === "discard" ? "Delete" : "";
  mark.hidden = !item.decision;
}

function repaint(item) {
  const i = indexOfUnit(item.unit_id);
  const cell = cells.get(item.unit_id);
  if (cell && i >= 0) paintCell(cell, item, i);
}

// Placeholder detection: /media/thumb serves its placeholder SVG with a 1x1 intrinsic size,
// so a loaded image with naturalWidth/Height <= 1 is the placeholder. A failed load counts as
// "no preview" too. Only a real, loaded thumbnail makes the cell eligible for dwell "seen".
function watchThumb(cell, img, item) {
  item.thumbState = "loading";
  const noPreview = () => {
    item.thumbState = "none";
    cell.classList.add("is-noprev");
    cell.querySelector(".cell-noprev").hidden = false;
  };
  img.addEventListener("load", () => {
    if (img.naturalWidth <= 1 && img.naturalHeight <= 1) { noPreview(); return; }
    item.thumbState = "ok";
    if (!item.seen && !localSeen.has(item.unit_id)) tracker.observe(cell, item.unit_id, cellReady(item));
  }, { once: true });
  img.addEventListener("error", noPreview, { once: true });
  if (!item.thumb) noPreview();
}

function makeCell(item, i) {
  const extra = (item.badges || []).map((c) => CATEGORY_LABELS[c] || c);
  const why = [item.reason, item.score != null ? fmtScore(item.score) : null, ...extra].filter(Boolean).join(" · ");
  const caption = item.name || why;
  const img = el("img", { src: item.thumb || "", alt: "", loading: "lazy", draggable: "false" });
  const cell = el("div", { class: "photo-cell", tabindex: "-1", "data-id": item.unit_id, title: [item.name, why].filter(Boolean).join(" · ") },
    img,
    el("span", { class: "cell-noprev", hidden: true }, "Pratinjau tidak tersedia"),
    el("span", { class: "seen-dot", hidden: true, title: "Sudah dilihat" }),
    el("span", { class: "cell-mark badge", hidden: true }),
    caption ? el("span", { class: "cell-caption" }, caption) : null);
  let clickTimer = null; // delay single-click so a double-click does not toggle first
  cell.addEventListener("click", () => {
    clearTimeout(clickTimer);
    clickTimer = setTimeout(() => { setFocus(indexOfUnit(item.unit_id), false); toggleKeep(item.unit_id); }, 250);
  });
  cell.addEventListener("dblclick", () => { clearTimeout(clickTimer); openFull(indexOfUnit(item.unit_id)); });
  cells.set(item.unit_id, cell);
  watchThumb(cell, img, item);
  paintCell(cell, item, i);
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
  showInspector();
}

// --- loading -----------------------------------------------------------------------------

function showRetry(err) {
  fail(err);
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
    fresh.forEach((it, n) => { it.idx = base + n; byId.set(it.unit_id, it); gridEl.append(makeCell(it, it.idx)); });
    if (viewerState) for (const it of fresh) viewerState.vitems.push(placeholder(it));
    sentinel.textContent = next < order.length ? "Memuat…" : "";
    if (!items.length && next >= order.length) sentinel.replaceChildren(el("p", { class: "empty" }, EMPTY[view]));
  } catch (e) {
    if (gen === generation) { loadFailed = true; showRetry(e); }
  } finally { if (gen === generation) loading = false; }
  // keep filling while the sentinel is still near the screen (the observer will not re-fire)
  if (gen === generation && next < order.length && !loadFailed
      && sentinel.getBoundingClientRect().top < window.innerHeight + 600) loadMore();
}

async function reload() {
  generation += 1;
  const gen = generation;
  for (const cell of cells.values()) tracker.unobserve(cell);
  cells.clear();
  byId.clear();
  items = []; order = []; next = 0; focus = 0;
  loading = false; loadFailed = false;
  gridEl.replaceChildren();
  sentinel.textContent = "Memuat…";
  paintToolbar();
  flushSeen(); // queued ids keep their dot and still reach the server
  try {
    const snap = await api("GET", `/api/review/grid/${encodeURIComponent(category)}/order?sort=${sort}&view=${view}`);
    if (gen !== generation) return;
    order = snap.unit_ids;
  } catch (e) {
    if (gen === generation) { loadFailed = true; showRetry(e); }
    return;
  }
  paintToolbar();
  await loadMore();
  if (gen === generation && items.length) setFocus(0, false);
  else if (gen === generation) showInspector();
}

if (typeof IntersectionObserver !== "undefined") {
  new IntersectionObserver((entries) => {
    if (entries.some((e) => e.isIntersecting)) loadMore();
  }, { root: scroller, rootMargin: "600px" }).observe(sentinel);
}

// --- decisions ---------------------------------------------------------------------------

async function decide(unitId, decision) {
  if (bar.applying) { toast("Apply sedang berjalan, tunggu sampai selesai."); return false; }
  if (busy) { toast("Masih menyimpan keputusan sebelumnya, coba lagi sebentar."); return false; }
  const item = byId.get(unitId);
  if (!item) return false;
  busy = true;
  try {
    await api("POST", "/api/review/decision", { unit_id: unitId, decision });
    item.decision = decision;
    repaint(item);
    if (focusedItem() === item) showInspector();
    bar.refresh();
    reviewNav.refresh();
    return true;
  } catch (e) { fail(e); return false; } finally { busy = false; }
}

function toggleKeep(unitId) {
  const item = byId.get(unitId);
  if (item) { queueSeenIfShown(item); decide(unitId, item.decision === "keep" ? null : "keep"); }
}

async function acceptFocused() {
  const it = focusedItem();
  if (!it) return;
  queueSeenIfShown(it);
  const ok = await decide(it.unit_id, null);
  if (ok && focus < items.length - 1) setFocus(focus + 1);
}

function decideFocused(decision) {
  const it = focusedItem();
  if (it) { queueSeenIfShown(it); decide(it.unit_id, decision); }
}

const gridKeys = {
  ArrowLeft: () => setFocus(focus - 1),
  ArrowRight: () => setFocus(focus + 1),
  ArrowUp: () => setFocus(Math.max(0, focus - columns())),
  ArrowDown: () => setFocus(Math.min(items.length - 1, focus + columns())),
  s: () => decideFocused("keep"),
  d: () => decideFocused("discard"),
  Enter: acceptFocused,
  " ": () => openFull(focus),
  i: () => toggleInspector(),
};
let unbindGrid = bindKeys(gridKeys);
window.addEventListener("pagehide", () => { unbindGrid(); unbindGrid = () => {}; });
window.addEventListener("pageshow", (ev) => { if (ev.persisted) unbindGrid = bindKeys(gridKeys); });

registerHelp([
  { title: "Review grid", keys: [["S", "Keep foto terpilih"], ["D", "Delete foto terpilih"],
    ["Enter", "Ikuti saran, lalu ke foto berikutnya"], ["Spasi", "Layar penuh (juga klik ganda)"],
    ["← → ↑ ↓", "Pindah foto"], ["I", "Buka/tutup panel info"], ["?", "Bantuan ini"]] },
  { title: "Penanda", keys: [["●", "Titik biru = sudah dilihat. Yang sudah dilihat dan tidak di-Keep pindah ke _sampah saat Apply."]] },
]);

// --- fullscreen viewer -------------------------------------------------------------------

function placeholder(it) {
  return {
    unit_id: it.unit_id,
    decision: it.decision,
    taken_at: it.taken_at,
    media: { kind: "photo", thumb: it.thumb },
    primary: { name: "" },
    flags: it.reason ? [{ category, reason: it.reason, score: it.score }] : [],
  };
}

async function ensureCard(n) {
  const vs = viewerState;
  if (!vs || n < 0 || n >= vs.vitems.length) return;
  const it = items[n];
  if (!it || vs.loaded.has(it.unit_id)) return;
  vs.loaded.add(it.unit_id);
  try {
    const card = await api("GET", `/api/review/unit/${encodeURIComponent(it.unit_id)}`);
    if (viewerState !== vs || vs.vitems[n] === undefined) return;
    card.decision = it.decision; // grid state is the freshest
    vs.vitems[n] = card;
    if (vs.handle && vs.handle.index === n) vs.handle.go(n);
  } catch (e) {
    vs.loaded.delete(it.unit_id);
    fail(e);
  }
}

async function openFull(i) {
  if (viewerState || i < 0 || i >= items.length) return;
  const vs = { vitems: items.map(placeholder), loaded: new Set(), handle: null };
  viewerState = vs;
  setFocus(i, false);
  queueSeen(items[i]);
  flushSeen();
  await ensureCard(i);
  if (viewerState !== vs) return;
  vs.handle = openViewer({
    mode: "single",
    items: vs.vitems,
    index: i,
    onDecide: async (card, decision) => {
      if (bar.applying) return;
      await decide(card.unit_id, decision === "suggest" ? null : decision);
      const it = byId.get(card.unit_id);
      if (it && viewerState === vs) {
        card.decision = it.decision;
        const vi = vs.vitems.findIndex((c) => c.unit_id === card.unit_id);
        if (vi >= 0) vs.vitems[vi].decision = it.decision;
        if (vs.handle && vs.handle.index === vi) vs.handle.go(vi);
      }
    },
    onIndex: (n) => {
      setFocus(n, false); // grid is behind the viewer: do not scroll it
      queueSeen(items[n]);
      flushSeen();
      ensureCard(n);
      ensureCard(n + 1);
      ensureCard(n - 1);
      if (n >= items.length - 6) loadMore();
    },
    onClose: () => {
      viewerState = null;
      const it = items[focus];
      const cell = it && cells.get(it.unit_id);
      if (cell && cell.scrollIntoView) cell.scrollIntoView({ block: "nearest" });
      tracker.flush(); // a full, still second on the grid again before anything counts
      flushSeen();
    },
  });
  ensureCard(i + 1);
  ensureCard(i - 1);
  if (i >= items.length - 6) loadMore();
}

// --- start -------------------------------------------------------------------------------

applySize();
toggleInspector(prefs.inspector);
reload().catch(fail);
