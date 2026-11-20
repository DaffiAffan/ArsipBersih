// Review grid page: wires the toolbar, the photo grid, the inspector, the fullscreen viewer and
// the status bar together. "Seen" comes only from the fullscreen viewer (D-033, D-039).
import { api, el } from "/static/core/api.js";
import { load, save } from "/static/core/storage.js";
import { bindKeys } from "/static/core/keys.js";
import { fmtNum } from "/static/core/format.js";
import { CATEGORY_LABELS } from "/static/components/labels.js";
import { reviewNav } from "/static/components/sidebar.js";
import { confirmDialog } from "/static/components/dialog.js";
import { registerHelp } from "/static/components/help.js";
import { toast } from "/static/components/toast.js";
import { offerUndo, clearDecisions } from "/static/components/undo.js";
import { createSeenQueue } from "/static/components/seen-dwell.js";
import { createInspectorLoader } from "/static/components/inspector.js";
import { mountToolbar } from "/static/components/toolbar.js";
import { mountStatusBar } from "/static/components/statusbar.js";
import { createPhotoGrid } from "/static/components/photo-grid.js";
import { createGridViewer } from "/static/components/grid-viewer.js";

const main = document.getElementById("rv-main");
const root = document.getElementById("grid-root");
const scroller = document.querySelector(".content");
const category = main.dataset.category;
const label = main.dataset.label || category;

const SEEN_CHUNK = 1000;
const SEEN_DELAY_MS = 1000;
const BULK_MAX = 500; // POST /api/review/decisions limit
const SIZES = [100, 150, 220]; // slider value -> minimum column width in px (0 = rapat, 2 = lega)
const VIEWS = [["all", "Semua"], ["unseen", "Belum dilihat"], ["seen", "Sudah dilihat"], ["keep", "Disimpan"], ["discard", "Dibuang"]];
const EMPTY = { all: "Tidak ada foto di kategori ini.", unseen: "Semua foto di sini sudah dibuka atau diputuskan.",
  seen: "Belum ada foto yang dibuka di layar penuh tanpa diputuskan.", keep: "Belum ada yang disimpan.", discard: "Belum ada yang dibuang." };
const PREF_KEY = `ab.grid.${category}`;
// Categories the AI fills: the grid offers "most uncertain guess first" (spec 12.3).
const AI_SORT_CATEGORIES = new Set(["dokumen-identitas", "dokumen-keuangan", "dokumen-kertas", "download-meme", "screenshot"]);
// Identity guesses are checked one by one: no bulk discard here (spec 7.7).
const NO_BULK = category === "dokumen-identitas";
const NO_BULK_TEXT = "Buang massal dimatikan di kategori Identitas. Cek tebakan satu per satu, lalu tekan D per foto.";
const BULK_LABEL = "Buang yang belum disimpan di layar ini";
const BUSY_APPLY = "Terapkan sedang berjalan, tunggu sampai selesai.";
const BUSY_SAVE = "Masih menyimpan keputusan sebelumnya, coba lagi sebentar.";
const LEFT_REVIEW_TEXT = "Tidak masuk kategori review mana pun lagi. Foto ini pindah ke Arsip saat Rapikan berikutnya.";

// --- preferences (view only; per category): size, captions, inspector ----------------------

const prefs = { size: 1, captions: false, inspector: false };
{
  const raw = load("local", PREF_KEY);
  if (raw && SIZES[raw.size] !== undefined) prefs.size = raw.size;
  if (raw && typeof raw.captions === "boolean") prefs.captions = raw.captions;
  if (raw && typeof raw.inspector === "boolean") prefs.inspector = raw.inspector;
}
const savePrefs = () => save("local", PREF_KEY, prefs);

let view = "all", sort = "score", busy = false;

function fail(err) { toast(err && err.message ? err.message : String(err), { kind: "error" }); }

// --- seen tracking (D-033): only once the full media loaded in the fullscreen viewer --------

let seenSent = false;
const seenQ = createSeenQueue({ chunk: SEEN_CHUNK, delayMs: SEEN_DELAY_MS, onSent: () => { seenSent = true; } });

function queueSeen(item) {
  if (!item || item.seen || !seenQ.add(item.unit_id)) return;
  item.seen = true; // queued ids get their dot right away and still reach the server
  grid.repaint(item);
}

// Resolves true only when every queued id reached the server.
function flushSeen() {
  return seenQ.flush().then((ok) => {
    if (seenSent) { seenSent = false; bar.refresh(); refreshCounts(); }
    return ok;
  });
}

// --- shell ---------------------------------------------------------------------------------

const inspEl = el("aside", { class: "inspector grid-inspector", "aria-label": "Info foto" });
const insp = createInspectorLoader(inspEl, { onError: fail });
const grid = createPhotoGrid({
  category, scroller, emptyText: (v) => EMPTY[v], onError: fail,
  onActivate: (item) => decide(item.unit_id, item.decision === "keep" ? null : "keep"),
  onOpen: (i) => viewer.open(i),
  onFocus: () => showInspector(),
  onAppend: (fresh) => viewer.append(fresh),
});

const helpText = main.dataset.help || "";
const toolbar = mountToolbar(root, {
  title: label,
  segments: VIEWS.map(([value, text]) => ({ value, label: text })),
  active: view,
  onSegment: (v) => { view = v; reload().catch(fail); },
  sort: { value: sort, options: [{ value: "score", label: "Terburuk dulu" }, { value: "date", label: "Tanggal" },
    ...(AI_SORT_CATEGORIES.has(category) ? [{ value: "ai_doubt", label: "Paling ragu dulu" }] : [])] },
  onSort: (v) => { sort = v; reload().catch(fail); },
  size: { value: prefs.size, max: SIZES.length - 1 },
  onSize: (v) => { prefs.size = v; savePrefs(); applySize(); },
  captions: prefs.captions,
  onCaptions: () => toggleCaptions(),
  onInfo: () => toggleInspector(),
  help: helpText ? { content: el("p", {}, helpText), storageKey: `ab.help.${category}` } : null,
  menuItems: [{ label: `${BULK_LABEL} (Shift+D)`, disabled: NO_BULK, title: NO_BULK ? NO_BULK_TEXT : null,
    onClick: () => { bulkDiscardOnScreen().catch(fail); } }],
});
root.classList.add("grid-root");
root.append(el("div", { class: "grid-layout" }, grid.el, inspEl));
const bar = mountStatusBar(root, {
  category, mode: "grid", label,
  hints: [["S", "simpan"], ["D", "buang"], ["Enter", "lanjut"], ["Spasi", "layar penuh"], ["?", "semua tombol"]],
  beforeApply: async () => {
    if (await flushSeen()) return true;
    toast("Tidak bisa mengirim daftar foto yang sudah dilihat. Coba lagi sebentar.", { kind: "error" });
    return false;
  },
  onApplied: () => { reload().catch(fail); },
});

function applySize() {
  toolbar.setSize(prefs.size);
  grid.setSize(SIZES[prefs.size]);
}

function toggleCaptions(on = !prefs.captions) {
  prefs.captions = on;
  savePrefs();
  toolbar.setCaptions(on);
  grid.setCaptions(on);
}

function toggleInspector(open = !prefs.inspector) {
  prefs.inspector = open;
  savePrefs();
  inspEl.hidden = !open;
  toolbar.setInfo(open);
  if (open) showInspector();
}

// "1.344 foto · 41 diputuskan": the whole category, from the counts the sidebar also uses.
async function refreshCounts() {
  const cats = await reviewNav.refresh();
  const mine = cats && cats.find((c) => c.category === category);
  if (mine) toolbar.setCount(`${fmtNum(mine.count)} foto · ${fmtNum(mine.done)} diputuskan`);
}

function showInspector() {
  if (!prefs.inspector) return;
  const it = grid.focused();
  insp.show(it ? it.unit_id : null, () => (it ? it.decision : null), (decision) => decide(it.unit_id, decision),
    (grp) => changeGroup(it.unit_id, grp));
}

async function reload() {
  flushSeen(); // queued ids keep their dot and still reach the server
  toolbar.setActive(view);
  refreshCounts();
  await grid.reload({ sort, view });
}

// --- decisions -----------------------------------------------------------------------------

function blocked() {
  if (bar.applying) { toast(BUSY_APPLY); return true; }
  if (busy) { toast(BUSY_SAVE); return true; }
  return false;
}

function changed() { bar.refresh(); refreshCounts(); }

async function decide(unitId, decision) {
  const item = grid.byId(unitId);
  if (!item || blocked()) return false;
  busy = true;
  try {
    await api("POST", "/api/review/decision", { unit_id: unitId, decision });
    item.decision = decision;
    grid.repaint(item);
    if (grid.focused() === item) showInspector();
    changed();
    return true;
  } catch (e) { fail(e); return false; } finally { busy = false; }
}

// Enter only moves on; it never changes or clears a decision.
function nextPhoto() { if (grid.focus < grid.items.length - 1) grid.setFocus(grid.focus + 1); }

function decideFocused(decision) { const it = grid.focused(); if (it) decide(it.unit_id, decision); }

// Saves the owner's AI group correction; the unit is reclassified on the server (no file moves).
// Resolves to the server reply, or null when nothing was saved.
async function changeGroup(unitId, grp) {
  if (!grid.byId(unitId) || blocked()) return null;
  busy = true;
  let res = null;
  try {
    res = await api("POST", "/api/review/ai-group", { unit_id: unitId, grp });
  } catch (e) { fail(e); return null; } finally { busy = false; }
  insp.forget(unitId);
  if (res.primary_category !== category) {
    const i = grid.remove(unitId);
    viewer.removed(i);
    toast(res.primary_category ? `Dipindah ke ${CATEGORY_LABELS[res.primary_category] || res.label || res.primary_category}` : LEFT_REVIEW_TEXT);
  } else {
    toast(`Kelompok diubah: ${res.ai ? res.ai.label : grp}. Tetap di kategori ini.`);
    if (grid.focused() && grid.focused().unit_id === unitId) showInspector();
    viewer.reloadCard(unitId);
  }
  changed();
  return res;
}

// --- bulk discard for the cells on screen (D-033), undoable from the toast (D-036) ----------

// The visible band of the grid: below the toolbar, above the status bar.
function visibleBand() {
  const box = scroller ? scroller.getBoundingClientRect() : { top: 0, bottom: window.innerHeight };
  const top = Math.max(box.top, toolbar.el.getBoundingClientRect().bottom);
  const bottom = Math.min(box.bottom, window.innerHeight, bar.el.getBoundingClientRect().top);
  return { top, bottom };
}

function markAll(ids, decision) {
  for (const id of ids) {
    const it = grid.byId(id);
    if (it) { it.decision = decision; grid.repaint(it); }
  }
  showInspector();
}

// Batalkan after Shift+D: only ids still discarded by it; while busy the toast comes back.
const UNDO = {
  busyText: () => (bar.applying ? BUSY_APPLY : busy ? BUSY_SAVE : null),
  // Ids not loaded now (tab switch, reload) are assumed to still hold the bulk decision;
  // the server only clears units that are still discarded (applied=0 filter).
  stillIds: (ids) => ids.filter((id) => { const it = grid.byId(id); return !it || it.decision === "discard"; }),
  run: async (ids) => {
    busy = true;
    try {
      const cleared = await clearDecisions(ids, (part) => markAll(part, null));
      toast(`${fmtNum(cleared)} keputusan dibatalkan`);
    } catch (e) { fail(e); } finally { busy = false; changed(); }
  },
};

async function bulkDiscardOnScreen() {
  if (viewer.isOpen || document.querySelector("dialog[open]")) return;
  if (NO_BULK) { toast(NO_BULK_TEXT); return; }
  if (blocked()) return;
  const targets = grid.onScreen(visibleBand()).slice(0, BULK_MAX);
  if (!targets.length) { toast("Tidak ada foto di layar yang bisa dibuang."); return; }
  const n = fmtNum(targets.length);
  const ok = await confirmDialog({
    title: BULK_LABEL,
    body: `Buang ${n} foto yang tampil di layar? Yang sudah disimpan tidak berubah. Semua bisa dibatalkan, juga setelah Terapkan.`,
    okLabel: `Buang ${n} foto`,
    danger: true,
  });
  if (!ok || blocked()) return;
  busy = true;
  try {
    // The server skips units decided elsewhere meanwhile and returns the ids it really stored.
    const res = await api("POST", "/api/review/decisions", { unit_ids: targets.map((it) => it.unit_id), decision: "discard" });
    markAll(res.unit_ids, "discard");
    offerUndo(`${fmtNum(res.recorded)} foto dibuang`, res.unit_ids, UNDO);
  } finally { busy = false; changed(); }
}

// --- fullscreen viewer ---------------------------------------------------------------------

const viewer = createGridViewer({
  category, grid, onError: fail, onChangeGroup: changeGroup,
  onDecide: async (unitId, decision) => {
    if (bar.applying) { toast(BUSY_APPLY); return false; }
    return decide(unitId, decision);
  },
  onShown: (item) => { queueSeen(item); flushSeen(); },
  onClose: () => { flushSeen(); },
});

// --- keys ----------------------------------------------------------------------------------

const gridKeys = {
  ArrowLeft: () => grid.setFocus(grid.focus - 1),
  ArrowRight: () => grid.setFocus(grid.focus + 1),
  ArrowUp: () => grid.setFocus(Math.max(0, grid.focus - grid.columns())),
  ArrowDown: () => grid.setFocus(Math.min(grid.items.length - 1, grid.focus + grid.columns())),
  s: () => decideFocused("keep"),
  d: (ev) => { if (ev && ev.shiftKey) bulkDiscardOnScreen().catch(fail); else decideFocused("discard"); },
  Enter: nextPhoto,
  " ": () => viewer.open(grid.focus),
  i: () => toggleInspector(),
  n: () => toggleCaptions(),
};
let unbindGrid = bindKeys(gridKeys);
window.addEventListener("pagehide", () => { unbindGrid(); unbindGrid = () => {}; });
window.addEventListener("pageshow", (ev) => { if (ev.persisted) unbindGrid = bindKeys(gridKeys); });

registerHelp([
  { title: "Review grid", keys: [["S", "Simpan foto terpilih"], ["D", "Buang foto terpilih"],
    ["Enter", "Lanjut ke foto berikutnya"], ["Shift+D", NO_BULK ? NO_BULK_TEXT : BULK_LABEL],
    ["Spasi", "Layar penuh (juga klik ganda)"], ["← → ↑ ↓", "Pindah foto"], ["I", "Buka/tutup panel info"],
    ["N", "Tampilkan/sembunyikan nama file"], ["?", "Bantuan ini"]] },
  { title: "Penanda", keys: [
    ["●", "Titik = sudah dibuka di layar penuh. Tetap di review saat Terapkan, kecuali kamu simpan atau buang."],
    ["✓", "Disimpan: pindah ke Arsip saat Terapkan."],
    ["✕", "Dibuang (foto redup): pindah ke Sampah saat Terapkan, bisa dibatalkan di Riwayat."]] },
]);

// --- start ---------------------------------------------------------------------------------

applySize();
grid.setCaptions(prefs.captions);
toggleInspector(prefs.inspector);
reload().catch(fail);
