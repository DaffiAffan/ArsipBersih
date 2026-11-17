// Review grid: tabs, density, inspector, floating Apply bar; "seen" only from the viewer (D-033).
import { api, el } from "/static/api.js";
import { bindKeys } from "/static/keys.js";
import { openViewer, CATEGORY_LABELS } from "/static/viewer.js";
import { reviewNav } from "/static/nav.js";
import { confirmDialog, fmtNum, registerHelp, toast } from "/static/ui.js";
import { createSeenQueue } from "/static/seen-dwell.js";
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
const BULK_MAX = 500; // POST /api/review/decisions limit
const SIZES = [100, 150, 220]; // slider value -> minimum column width in px (0 = cepat, 2 = santai)
const VIEWS = [["all", "Semua"], ["unseen", "Belum diputuskan"], ["keep", "Keep"]]; // "unseen": undecided, never opened in detail
const EMPTY = { all: "Tidak ada item di kategori ini.", unseen: "Semua foto di sini sudah diputuskan atau sudah dibuka di detail.", keep: "Belum ada yang di-Keep." };
const PREF_KEY = `ab.grid.${category}`;
// Categories the AI fills: the grid offers "most uncertain guess first" (spec 12.3).
const AI_SORT_CATEGORIES = new Set(["dokumen-identitas", "dokumen-keuangan", "dokumen-kertas", "download-meme", "screenshot"]);
// Identity guesses are checked one by one: no bulk Delete here (spec 7.7).
const NO_BULK = category === "dokumen-identitas";
const NO_BULK_TEXT = "Delete massal dimatikan di kategori Identitas. Cek tebakan satu per satu, lalu tekan D per foto.";

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

// --- seen tracking (D-033) -------------------------------------------------------------
// A unit counts as seen only once its full media has loaded in the fullscreen viewer (onShown).
// Thumbnails in the grid and S/D/Enter/click never mark anything seen.

const localSeen = new Set(); // queued or sent this page: drawn with the dot right away
let seenSent = false;
const seenQ = createSeenQueue({
  chunk: SEEN_CHUNK, delayMs: SEEN_DELAY_MS,
  onSent: (ids) => { seenSent = true; for (const id of ids) { const it = byId.get(id); if (it) it.seen = true; } },
});

function queueSeen(item) {
  if (!item || item.seen || !seenQ.add(item.unit_id)) return;
  localSeen.add(item.unit_id);
  repaint(item);
}

// Resolves true only when every queued id reached the server.
function flushSeen() {
  return seenQ.flush().then((ok) => {
    if (seenSent) { seenSent = false; bar.refresh(); reviewNav.refresh(); }
    return ok;
  });
}

// --- shell -----------------------------------------------------------------------------

const countEl = el("span", { class: "grid-count muted" });
const tabs = VIEWS.map(([v, text]) => el("button", {
  type: "button", class: "pill", role: "tab", "data-view": v, onclick: () => { if (view !== v) { view = v; reload(); } },
}, text));
const sortSel = el("select", { class: "grid-select", "aria-label": "Urutan", onchange: (e) => { sort = e.target.value; reload(); } },
  el("option", { value: "score" }, "Skor (terburuk dulu)"),
  el("option", { value: "date" }, "Tanggal"),
  AI_SORT_CATEGORIES.has(category) ? el("option", { value: "ai_doubt" }, "Paling ragu dulu") : null);
const sizeInput = el("input", { type: "range", min: "0", max: String(SIZES.length - 1), step: "1", "aria-label": "Ukuran thumbnail",
  oninput: (e) => { prefs.size = Number(e.target.value); savePrefs(); applySize(); } });
const infoBtn = el("button", { type: "button", class: "btn btn-ghost", onclick: () => toggleInspector() },
  "Info ", el("kbd", { class: "kbd" }, "I"));
const BULK_LABEL = "Delete yang belum di-Keep di layar ini";
const bulkBtn = el("button", { type: "button", class: "btn btn-ghost grid-bulk", title: NO_BULK ? NO_BULK_TEXT : `${BULK_LABEL} (Shift+D)`,
  "aria-disabled": NO_BULK ? "true" : null,
  onclick: () => { bulkDeleteOnScreen().catch(fail); } },
  BULK_LABEL, " ", el("kbd", { class: "kbd" }, "Shift D"));
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
    bulkBtn,
    infoBtn);
root.replaceChildren(
  toolbarEl,
  el("p", { class: "grid-hint muted" },
    el("kbd", { class: "kbd" }, "S"), " keep · ", el("kbd", { class: "kbd" }, "D"), " delete · ",
    ...(NO_BULK ? [] : [el("kbd", { class: "kbd" }, "Shift D"), " delete yang tampil · "]),
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
  insp.show(it ? it.unit_id : null, () => (it ? it.decision : null), (decision) => decide(it.unit_id, decision),
    (grp) => changeGroup(it.unit_id, grp));
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
// "no preview" too. Only a real, loaded thumbnail makes the cell a target of the bulk Delete.
function watchThumb(cell, img, item) {
  item.thumbState = "loading";
  const noPreview = () => {
    item.thumbState = "none";
    cell.classList.add("is-noprev");
    cell.querySelector(".cell-noprev").hidden = false;
  };
  img.addEventListener("load", () => {
    if (!img.getAttribute("src")) return; // released meanwhile
    if (img.naturalWidth <= 1 && img.naturalHeight <= 1) { noPreview(); return; }
    item.thumbState = "ok";
  });
  img.addEventListener("error", () => { if (img.getAttribute("src")) noPreview(); });
  if (!item.thumb) noPreview();
}

// Cells more than ~2 screens from the visible area drop their image (memory on 8 GB RAM) and
// reload it when they come back. The cell keeps its size (aspect-ratio), so scrolling never jumps.
// A released cell is not "thumbnail shown", so the bulk Delete never targets it.
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

function makeCell(item, i) {
  const extra = (item.badges || []).map((c) => CATEGORY_LABELS[c] || c);
  const why = [item.reason, item.score != null ? fmtScore(item.score) : null, ...extra].filter(Boolean).join(" · ");
  const caption = item.name || why;
  const img = el("img", { src: item.thumb || "", alt: "", loading: "lazy", draggable: "false" });
  const cell = el("div", { class: "photo-cell", tabindex: "-1", "data-id": item.unit_id, title: [item.name, why].filter(Boolean).join(" · ") },
    img,
    el("span", { class: "cell-noprev", hidden: true }, "Pratinjau tidak tersedia"),
    el("span", { class: "seen-dot", hidden: true, title: "Sudah dibuka di detail" }),
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
  if (thumbObserver) thumbObserver.observe(cell);
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
  if (thumbObserver) thumbObserver.disconnect();
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
  if (item) decide(unitId, item.decision === "keep" ? null : "keep");
}

async function acceptFocused() {
  const it = focusedItem();
  if (!it) return;
  const ok = await decide(it.unit_id, null);
  if (ok && focus < items.length - 1) setFocus(focus + 1);
}

function decideFocused(decision) {
  const it = focusedItem();
  if (it) decide(it.unit_id, decision);
}

// --- AI group correction ("Ubah kelompok") -------------------------------------------------

// Drops a unit that left this category from every list of the page (grid, order, viewer).
function removeItem(unitId) {
  const i = indexOfUnit(unitId);
  if (i < 0) return;
  const cell = cells.get(unitId);
  if (cell) { if (thumbObserver) thumbObserver.unobserve(cell); cell.remove(); }
  cells.delete(unitId);
  byId.delete(unitId);
  items.splice(i, 1);
  items.forEach((it, n) => { it.idx = n; });
  const oi = order.indexOf(unitId);
  if (oi >= 0) { order.splice(oi, 1); if (oi < next) next -= 1; }
  paintToolbar();
  const vs = viewerState;
  if (vs) {
    vs.vitems.splice(i, 1);
    vs.loaded.delete(unitId);
  }
  if (!items.length) {
    focus = 0;
    if (next >= order.length) sentinel.replaceChildren(el("p", { class: "empty" }, EMPTY[view]));
    if (vs && vs.handle) vs.handle.close();
    showInspector();
    loadMore();
    return;
  }
  if (focus > i) focus -= 1;
  setFocus(Math.min(focus, items.length - 1), false);
  if (vs && vs.handle) {
    const at = Math.min(vs.handle.index > i ? vs.handle.index - 1 : vs.handle.index, items.length - 1);
    vs.handle.go(at);
  }
}

const LEFT_REVIEW_TEXT = "Tidak masuk kategori review mana pun lagi. Foto ini pindah ke Arsip saat Rapikan berikutnya.";

// Saves the owner's correction; the unit is reclassified on the server (no file moves).
// Resolves to the server reply, or null when nothing was saved.
async function changeGroup(unitId, grp) {
  if (bar.applying) { toast("Apply sedang berjalan, tunggu sampai selesai."); return null; }
  if (busy) { toast("Masih menyimpan keputusan sebelumnya, coba lagi sebentar."); return null; }
  if (!byId.has(unitId)) return null;
  busy = true;
  let res = null;
  try {
    res = await api("POST", "/api/review/ai-group", { unit_id: unitId, grp });
  } catch (e) { fail(e); return null; } finally { busy = false; }
  insp.forget(unitId);
  const vs = viewerState;
  if (res.primary_category !== category) {
    removeItem(unitId);
    toast(res.primary_category ? `Dipindah ke ${CATEGORY_LABELS[res.primary_category] || res.label || res.primary_category}` : LEFT_REVIEW_TEXT);
  } else {
    toast(`Kelompok diubah: ${res.ai ? res.ai.label : grp}. Tetap di kategori ini.`);
    if (focusedItem() && focusedItem().unit_id === unitId) showInspector();
    if (vs) { // reload the fullscreen card so its info panel shows the correction
      vs.loaded.delete(unitId);
      ensureCard(indexOfUnit(unitId));
    }
  }
  bar.refresh();
  reviewNav.refresh();
  return res;
}

// --- bulk Delete for the cells on screen (D-033) -----------------------------------------

// The visible band of the grid: below the sticky toolbar, above the floating Apply bar.
function visibleBand() {
  const box = scroller ? scroller.getBoundingClientRect() : { top: 0, bottom: window.innerHeight };
  const top = Math.max(box.top, toolbarEl.getBoundingClientRect().bottom);
  let bottom = Math.min(box.bottom, window.innerHeight);
  const fb = root.querySelector(".floatbar");
  if (fb && !fb.hidden && getComputedStyle(fb).display !== "none") { // fixed: no offsetParent
    const r = fb.getBoundingClientRect();
    if (r.height > 0) bottom = Math.min(bottom, r.top);
  }
  return { top, bottom };
}

// Undecided units whose real thumbnail is loaded and at least half visible in the band.
function onScreenTargets() {
  const band = visibleBand();
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

// The server skips units decided elsewhere meanwhile: read back what it actually stored.
async function refreshDecisions(ids) {
  for (let i = 0; i < ids.length; i += BULK_MAX) {
    const data = await api("POST", `/api/review/grid/${encodeURIComponent(category)}/items`, { unit_ids: ids.slice(i, i + BULK_MAX) });
    for (const fresh of data.items) {
      const it = byId.get(fresh.unit_id);
      if (it) { it.decision = fresh.decision; repaint(it); }
    }
  }
}

function bulkBlocked() {
  if (bar.applying) { toast("Apply sedang berjalan, tunggu sampai selesai."); return true; }
  if (busy) { toast("Masih menyimpan keputusan sebelumnya, coba lagi sebentar."); return true; }
  return false;
}

async function bulkDeleteOnScreen() {
  if (viewerState || document.querySelector("dialog[open]")) return;
  if (NO_BULK) { toast(NO_BULK_TEXT); return; }
  if (bulkBlocked()) return;
  const targets = onScreenTargets().slice(0, BULK_MAX);
  if (!targets.length) { toast("Tidak ada foto di layar yang bisa di-Delete."); return; }
  const n = fmtNum(targets.length);
  const ok = await confirmDialog({
    title: BULK_LABEL,
    body: `Delete ${n} foto yang tampil di layar? Yang sudah di-Keep tidak berubah. Semua bisa di-Undo setelah Apply.`,
    okLabel: `Delete ${n} foto`,
    danger: true,
  });
  if (!ok || bulkBlocked()) return;
  busy = true;
  const gen = generation;
  const ids = targets.map((it) => it.unit_id);
  try {
    const res = await api("POST", "/api/review/decisions", { unit_ids: ids, decision: "discard" });
    if (gen !== generation) return;
    try {
      await refreshDecisions(ids);
    } catch (_) {
      if (res.recorded === ids.length) for (const it of targets) { it.decision = "discard"; repaint(it); }
    }
    if (gen !== generation) return;
    showInspector();
    toast(`${fmtNum(res.recorded)} foto ditandai Delete`);
  } finally {
    busy = false;
    bar.refresh();
    reviewNav.refresh();
  }
}

const gridKeys = {
  ArrowLeft: () => setFocus(focus - 1),
  ArrowRight: () => setFocus(focus + 1),
  ArrowUp: () => setFocus(Math.max(0, focus - columns())),
  ArrowDown: () => setFocus(Math.min(items.length - 1, focus + columns())),
  s: () => decideFocused("keep"),
  d: (ev) => { if (ev && ev.shiftKey) bulkDeleteOnScreen().catch(fail); else decideFocused("discard"); },
  Enter: acceptFocused,
  " ": () => openFull(focus),
  i: () => toggleInspector(),
};
let unbindGrid = bindKeys(gridKeys);
window.addEventListener("pagehide", () => { unbindGrid(); unbindGrid = () => {}; });
window.addEventListener("pageshow", (ev) => { if (ev.persisted) unbindGrid = bindKeys(gridKeys); });

registerHelp([
  { title: "Review grid", keys: [["S", "Keep foto terpilih"], ["D", "Delete foto terpilih"],
    ["Enter", "Ikuti saran, lalu ke foto berikutnya"], ["Shift+D", NO_BULK ? NO_BULK_TEXT : BULK_LABEL],
    ["Spasi", "Layar penuh (juga klik ganda)"],
    ["← → ↑ ↓", "Pindah foto"], ["I", "Buka/tutup panel info"], ["?", "Bantuan ini"]] },
  { title: "Penanda", keys: [["●", "Titik biru = sudah dibuka di detail. Saat Apply, yang dibuang hanya yang di-Delete atau sudah dibuka dan tidak di-Keep; sisanya tetap di review."]] },
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
  await ensureCard(i);
  if (viewerState !== vs) return;
  vs.handle = openViewer({
    mode: "single",
    items: vs.vitems,
    index: i,
    onDecide: async (card, decision) => {
      if (bar.applying) { toast("Apply sedang berjalan, tunggu sampai selesai."); return false; }
      const ok = await decide(card.unit_id, decision === "suggest" ? null : decision);
      const it = byId.get(card.unit_id);
      if (it && viewerState === vs) {
        card.decision = it.decision;
        const vi = vs.vitems.findIndex((c) => c.unit_id === card.unit_id);
        if (vi >= 0) vs.vitems[vi].decision = it.decision;
        if (vs.handle && vs.handle.index === vi) vs.handle.go(vi);
      }
      return ok;
    },
    onIndex: (n) => {
      setFocus(n, false); // grid is behind the viewer: do not scroll it
      ensureCard(n);
      ensureCard(n + 1);
      ensureCard(n - 1);
      if (n >= items.length - 6) loadMore();
    },
    onShown: (card) => {
      queueSeen(byId.get(card.unit_id));
      flushSeen();
    },
    onChangeGroup: (card, grp) => changeGroup(card.unit_id, grp),
    onClose: () => {
      viewerState = null;
      const it = items[focus];
      const cell = it && cells.get(it.unit_id);
      if (cell && cell.scrollIntoView) cell.scrollIntoView({ block: "nearest" });
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
