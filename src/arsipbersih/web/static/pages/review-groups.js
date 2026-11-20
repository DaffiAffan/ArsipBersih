// Review of groups (duplikat-persis, duplikat-versi, mirip-beruntun): compact group list on the
// left, large comparison on the right, status bar below. Decisions are only applied by Terapkan.
import { api, el } from "/static/core/api.js";
import { fmtNum } from "/static/core/format.js";
import { bindKeys, onClick } from "/static/core/keys.js";
import { openViewer } from "/static/components/viewer.js";
import { reviewNav } from "/static/components/sidebar.js";
import { confirmDialog } from "/static/components/dialog.js";
import { registerHelp } from "/static/components/help.js";
import { toast } from "/static/components/toast.js";
import { offerUndo, clearDecisions } from "/static/components/undo.js";
import { createInspectorLoader } from "/static/components/inspector.js";
import { mountToolbar } from "/static/components/toolbar.js";
import { mountStatusBar } from "/static/components/statusbar.js";
import { mountGroupList } from "/static/components/group-list.js";
import { renderCompare, locked } from "/static/components/compare.js";

const main = document.getElementById("rv-main");
const root = document.getElementById("group-root");
const category = main.dataset.category;
const label = main.dataset.label || category;

const DETAIL_DELAY_MS = 80;
const BUSY_APPLY = "Terapkan sedang berjalan, tunggu sampai selesai.";
const BUSY_SAVE = "Masih menyimpan keputusan sebelumnya, coba lagi sebentar.";
const LOADING = "Masih memuat grup…";

let activeId = null; // group chosen in the list
let detail = null; // its full detail, once on screen
let cmp = null; // renderCompare handle for `detail`
let mfocus = 0; // active photo in the comparison
let busy = false, viewerOpen = false, infoOpen = false, detailTimer = null;
const detailCache = new Map();

function fail(err) { toast(err && err.message ? err.message : String(err), { kind: "error" }); }

// --- shell -----------------------------------------------------------------------------

const acceptAllBtn = el("button", { type: "button", class: "btn", disabled: true, onclick: onClick(() => { acceptLoaded().catch(fail); }) }, "Ikuti saran");
const zoomBtn = el("button", { type: "button", class: "btn btn-ghost", title: "Zoom bareng (Z)", "aria-pressed": "false",
  onclick: onClick(() => toggleZoom()) }, "Zoom bareng ", el("kbd", { class: "kbd" }, "Z"));
const helpText = main.dataset.help || "";
const toolbar = mountToolbar(root, {
  title: label, actions: [acceptAllBtn, zoomBtn], onInfo: () => toggleInfo(),
  help: helpText ? { content: el("p", {}, helpText), storageKey: `ab.help.${category}` } : null,
});
const layout = el("div", { class: "grp-layout" }), cmpEl = el("div", { class: "grp-compare" });
const inspEl = el("aside", { class: "inspector grp-inspector", "aria-label": "Info foto", hidden: true });
const insp = createInspectorLoader(inspEl, { onError: fail });
root.classList.add("grp-root");
root.append(layout);
const list = mountGroupList(layout, { kind: category, onSelect: (id) => showGroup(id), onLoadedPage: (ids) => loaded(ids) });
layout.append(cmpEl, inspEl);
const bar = mountStatusBar(root, {
  category, mode: "group", label,
  hints: [["↑↓", "grup"], ["←→", "foto"], ["S", "simpan"], ["D", "buang"], ["Enter", "ikuti saran"], ["?", "semua tombol"]],
  // The shown comparison is stale after Terapkan: re-read the list, then the active group.
  onApplied: () => { detailCache.clear(); list.refresh().then(() => { if (activeId) showGroup(activeId); }).catch(fail); },
});

function loaded(ids) {
  acceptAllBtn.textContent = `Ikuti saran ${fmtNum(ids.length)} grup ini`;
  acceptAllBtn.disabled = !ids.length;
  if (!ids.length) { activeId = null; clearDetail(); cmpEl.replaceChildren(); } else if (!ids.includes(activeId)) list.select(ids[0]);
}

// "7 grup · 7 diputuskan": the whole category, from the counts the sidebar also uses.
async function refreshCounts() {
  const cats = await reviewNav.refresh();
  const mine = cats && cats.find((c) => c.category === category);
  if (mine) toolbar.setCount(`${fmtNum(mine.count)} grup · ${fmtNum(mine.done)} diputuskan`);
}

function changed() { bar.refresh(); refreshCounts(); }

function toggleInfo(open = !infoOpen) {
  infoOpen = open; inspEl.hidden = !open; toolbar.setInfo(open); showInfo();
}

function showInfo() {
  if (!infoOpen) return;
  const i = mfocus;
  const m = detail && detail.members[i];
  insp.show(m ? m.unit_id : null, () => (m ? m.decision : null), (want) => decide(i, want));
}

// --- active group --------------------------------------------------------------------------

// Forget the shown group first, so a key press while the next one loads never lands on the
// previous group's members (readyGroup refuses instead).
function clearDetail() {
  if (cmp) cmp.destroy();
  zoomBtn.setAttribute("aria-pressed", "false");
  clearTimeout(detailTimer);
  detail = null; cmp = null; mfocus = 0;
}

function readyGroup() {
  if (detail && detail.id === activeId) return detail;
  if (activeId) toast(LOADING);
  return null;
}

function showGroup(id) {
  clearDetail();
  activeId = id;
  cmpEl.replaceChildren(el("p", { class: "empty" }, "Memuat…"));
  if (detailCache.has(id)) { renderDetail(detailCache.get(id)); return; }
  detailTimer = setTimeout(async () => {
    try {
      const d = await api("GET", `/api/review/group/${encodeURIComponent(id)}`);
      detailCache.set(id, d);
      if (detailCache.size > 20) detailCache.delete(detailCache.keys().next().value);
      if (activeId === id && !detail) renderDetail(d);
    } catch (e) { fail(e); }
  }, DETAIL_DELAY_MS);
}

function renderDetail(d) {
  detail = d;
  const first = d.members.findIndex((m) => !m.is_keeper && !locked(m));
  mfocus = first >= 0 ? first : 0;
  cmp = renderCompare(cmpEl, d, {
    activeIndex: mfocus,
    onDecide: (uid, want) => decide(d.members.findIndex((m) => m.unit_id === uid), want),
    onActivate: (i) => setMember(i),
    onAccept: () => acceptActive(),
    onClear: () => clearActive(),
  });
  showInfo();
}

function setMember(i) {
  if (!cmp || i < 0 || i >= detail.members.length) return;
  mfocus = i;
  cmp.setActive(i);
  showInfo();
}

function toggleZoom() {
  if (!readyGroup()) return;
  const on = cmp.toggleZoom();
  zoomBtn.setAttribute("aria-pressed", String(Boolean(on)));
  if (on === null) toast("Tidak ada foto untuk di-zoom.");
  else if (on) toast("Zoom bareng aktif: scroll untuk zoom, seret untuk geser, klik ganda untuk reset.");
}

// --- decisions -----------------------------------------------------------------------------

function blocked() {
  if (bar.applying) { toast(BUSY_APPLY); return true; }
  if (busy) { toast(BUSY_SAVE); return true; }
  return false;
}

// Stores a saved decision by id in every copy (list row, cached and shown detail): the list and
// the detail may have changed while the request was in flight.
function record(gid, uid, decision) {
  for (const d of new Set([detailCache.get(gid), detail && detail.id === gid ? detail : null])) {
    const m = d && d.members.find((x) => x.unit_id === uid);
    if (m) m.decision = decision;
  }
  list.setDecision(gid, uid, decision);
}

function repaintActive() { if (cmp) cmp.paint(); showInfo(); }

async function decide(i, decision) {
  const d = readyGroup();
  const m = d && d.members[i];
  if (!m) return false;
  if (locked(m)) { toast("Foto ini sudah diproses, tidak bisa diubah."); return false; }
  if (blocked()) return false;
  busy = true;
  try {
    await api("POST", "/api/review/decision", { unit_id: m.unit_id, decision });
    record(d.id, m.unit_id, decision);
    repaintActive();
    changed();
    return true;
  } catch (e) { fail(e); return false; } finally { busy = false; }
}

// "Batalkan keputusan": the group goes back to undecided (only decisions not applied yet).
async function clearActive() {
  const d = readyGroup();
  if (!d || blocked()) return;
  const ids = d.members.filter((m) => !locked(m) && m.decision != null).map((m) => m.unit_id);
  busy = true;
  try {
    await clearDecisions(ids, (part) => { for (const uid of part) record(d.id, uid, null); repaintActive(); });
  } catch (e) { fail(e); } finally { busy = false; changed(); }
}

async function accept(gids) {
  const res = await api("POST", "/api/review/accept", { group_ids: gids });
  const want = list.suggested(gids, res.unit_ids);
  for (const [uid, w] of want) record(w.gid, uid, w.decision);
  repaintActive();
  return want;
}

async function acceptActive() {
  const d = readyGroup();
  if (!d || blocked()) return;
  busy = true;
  try { await accept([d.id]); } catch (e) { fail(e); return; } finally { busy = false; changed(); }
  moveGroup(1, "Itu grup terakhir di kategori ini.");
}

// "Ikuti saran N grup ini": every loaded group, undoable from the toast (D-036, §10a).
async function acceptLoaded() {
  const gids = list.loadedIds();
  if (!gids.length || blocked()) return;
  const n = fmtNum(gids.length);
  const ok = await confirmDialog({
    title: `Ikuti saran ${n} grup ini?`,
    body: [`${n} grup yang dimuat mengikuti saran untuk foto yang belum diputuskan.`,
      "Keputusan yang sudah kamu buat tidak berubah. Belum ada file yang dipindah sebelum Terapkan."],
    okLabel: "Ikuti saran",
  });
  if (!ok || blocked()) return;
  busy = true;
  try {
    const want = await accept(gids);
    const groups = new Set([...want.values()].map((w) => w.gid)).size;
    offerUndo(groups ? `${fmtNum(groups)} grup mengikuti saran` : "Semua grup ini sudah diputuskan.", [...want.keys()], {
      busyText: () => (bar.applying ? BUSY_APPLY : busy ? BUSY_SAVE : null),
      // Only what `accept` stored and still stands: a decision changed afterwards survives.
      stillIds: (ids) => ids.filter((uid) => list.decisionOf(want.get(uid).gid, uid) === want.get(uid).decision),
      run: (ids) => { undoAccept(ids, want).catch(fail); },
    });
  } finally { busy = false; changed(); }
}

async function undoAccept(ids, want) {
  busy = true;
  try {
    // Local state follows each chunk the server confirmed, even if a later chunk fails.
    const cleared = await clearDecisions(ids, (part) => { for (const uid of part) record(want.get(uid).gid, uid, null); repaintActive(); });
    toast(`${fmtNum(cleared)} keputusan dibatalkan`);
  } finally { busy = false; changed(); }
}

// --- fullscreen compare ------------------------------------------------------------------

function openFull() {
  const d = !viewerOpen && readyGroup();
  if (!d) return;
  const members = d.members;
  const keeper = members.find((m) => m.is_keeper) || members[0];
  const others = members.filter((m) => m !== keeper);
  if (!others.length) return;
  viewerOpen = true;
  openViewer({
    mode: "compare",
    items: others.map((m) => [keeper, m]),
    index: Math.max(0, others.indexOf(members[mfocus])),
    onDecide: async (item, decision) => {
      if (detail !== d) { toast(LOADING); return false; }
      return decide(members.indexOf(item), decision === "suggest" ? (item.is_keeper ? "keep" : "discard") : decision);
    },
    onClose: () => { viewerOpen = false; repaintActive(); },
  });
}

// --- keys ----------------------------------------------------------------------------------

// ↑↓ between groups; at the end of the list the next page is loaded first.
async function moveGroup(step, atEnd = null) {
  if (list.loading) { toast(LOADING); return; }
  const n = list.loadedIds().indexOf(activeId) + step;
  if (n < 0) return;
  if (n >= list.loadedIds().length && step > 0 && list.hasMore) {
    try { await list.loadMore(); } catch (e) { fail(e); return; }
  }
  const ids = list.loadedIds();
  if (n < ids.length) list.select(ids[n]); else if (atEnd) toast(atEnd);
}

const keyMap = {
  ArrowUp: () => moveGroup(-1), ArrowDown: () => moveGroup(1),
  ArrowLeft: () => setMember(mfocus - 1), ArrowRight: () => setMember(mfocus + 1),
  s: () => decide(mfocus, "keep"), d: (ev) => { if (!(ev && ev.shiftKey)) decide(mfocus, "discard"); },
  Enter: () => acceptActive(), " ": () => openFull(), z: () => toggleZoom(), i: () => toggleInfo(),
};
let unbindKeys = bindKeys(keyMap);
window.addEventListener("pagehide", () => { unbindKeys(); unbindKeys = () => {}; });
window.addEventListener("pageshow", (ev) => { if (ev.persisted) unbindKeys = bindKeys(keyMap); });

registerHelp([
  { title: "Review grup", keys: [["↑ ↓", "Grup sebelumnya / berikutnya (di ujung daftar memuat grup berikutnya)"],
    ["← →", "Pindah foto di grup"], ["S", "Simpan foto terpilih"], ["D", "Buang foto terpilih"],
    ["Enter", "Ikuti saran untuk grup ini, lalu ke grup berikutnya"], ["Spasi", "Layar penuh (banding dengan yang disarankan disimpan)"],
    ["Z", "Zoom bareng: semua foto di grup di-zoom dan digeser serentak"], ["I", "Buka/tutup panel info"], ["?", "Bantuan ini"]] },
  { title: "Penanda", keys: [["Saran: simpan", "Hanya saran. Foto tetap terang sampai kamu memutuskan."],
    ["✓", "Disimpan: pindah ke Arsip saat Terapkan."], ["✕", "Dibuang (foto redup): pindah ke Sampah saat Terapkan."],
    ["sebagian", "Baru sebagian foto di grup yang diputuskan"]] }]);

// --- start ---------------------------------------------------------------------------------

refreshCounts();
list.loadMore().catch(fail);
