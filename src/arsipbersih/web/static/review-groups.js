// Review of groups (duplikat-persis, duplikat-versi, mirip-beruntun): group list on the left,
// members of the active group side by side on the right. Decisions are never applied here;
// the floating bar does that (floatbar.js).
import { api, el, fmtBytes, LOCATION_LABELS } from "/static/api.js";
import { bindKeys } from "/static/keys.js";
import { openViewer } from "/static/viewer.js";
import { reviewNav } from "/static/nav.js";
import { confirmDialog, fmtNum, registerHelp, toast } from "/static/ui.js";
import { createZoom } from "/static/zoom.js";
import { mountFloatBar } from "/static/floatbar.js";

const main = document.getElementById("rv-main");
const root = document.getElementById("group-root");
const category = main.dataset.category;
const label = main.dataset.label || category;

const MAX_MINI = 5;
const DETAIL_DELAY_MS = 80;

let page = 1;
let data = { groups: [], page: 1, pages: 1, total: 0 };
let focus = 0; // active group (index in data.groups)
let mfocus = 0; // focused member of the active group
let detail = null; // full detail of the active group
let cards = []; // [{ m, node, pane, paint }]
let zoom = null;
let busy = false;
let viewerOpen = false;
let wantGroup = null; // id of the group whose detail is wanted
let detailTimer = null;
let wantLast = false; // after a page change from the top edge: land on the last group
const detailCache = new Map();

// After a pointer click the button would keep focus, and keys.js ignores Enter/Space on buttons.
// Drop focus for mouse clicks (detail > 0); keyboard activation (Tab + Enter/Space) keeps it.
function pt(fn) {
  return (ev) => {
    if (ev && ev.detail > 0 && ev.currentTarget && ev.currentTarget.blur) ev.currentTarget.blur();
    return fn();
  };
}

function fail(err) { toast(err && err.message ? err.message : String(err), { kind: "error" }); }

// A member can be decided unless its decision is already applied or it sits in the trash.
function locked(m) { return Boolean(m.applied) || m.location === "trash"; }

// Same rule as the server: an archived suggested keeper and trashed members need no decision.
function statusOf(g) {
  const needing = g.members.filter((m) => m.location !== "trash"
    && !(m.location === "archive" && m.unit_id === g.keeper_id));
  if (!needing.length) return "all";
  const n = needing.filter((m) => m.decision != null).length;
  return n === 0 ? "none" : n === needing.length ? "all" : "partial";
}

// --- shell -----------------------------------------------------------------------------

const countEl = el("span", { class: "grid-count muted" });
const acceptAllBtn = el("button", { type: "button", class: "btn btn-secondary", onclick: pt(() => acceptAll()) }, "Ikuti saran semua di halaman ini");
const listEl = el("div", { class: "grp-list", role: "listbox", "aria-label": "Daftar grup" });
const pagerEl = el("div", { class: "grp-pager" });
const reasonEl = el("div", { class: "grp-reason", hidden: true });
const metaEl = el("div", { class: "grp-meta muted" });
const actionsEl = el("div", { class: "grp-actions" });
const cardsEl = el("div", { class: "cmp-cards" });
const detailEl = el("section", { class: "grp-detail", "aria-label": "Perbandingan" }, metaEl, reasonEl, actionsEl, cardsEl);

root.classList.add("grp-root");
root.replaceChildren(
  el("div", { class: "grid-toolbar" }, countEl, el("span", { class: "grid-spacer" }), acceptAllBtn),
  el("p", { class: "grid-hint muted" },
    el("kbd", { class: "kbd" }, "↑↓"), " grup · ", el("kbd", { class: "kbd" }, "←→"), " anggota · ",
    el("kbd", { class: "kbd" }, "S"), " keep · ", el("kbd", { class: "kbd" }, "D"), " delete · ",
    el("kbd", { class: "kbd" }, "Enter"), " ikuti saran · ", el("kbd", { class: "kbd" }, "Spasi"), " layar penuh · ",
    el("kbd", { class: "kbd" }, "Z"), " zoom bareng · ", el("kbd", { class: "kbd" }, "?"), " bantuan"),
  el("div", { class: "grp-layout" }, el("div", { class: "grp-side" }, listEl, pagerEl), detailEl));

const bar = mountFloatBar(root, {
  category, mode: "group", label,
  onApplied: () => { detailCache.clear(); loadList().catch(fail); },
});

// --- group list ------------------------------------------------------------------------

function fmtRange(g) {
  if (!g.taken_from) return "Tanggal tidak diketahui";
  const a = new Date(g.taken_from);
  return Number.isNaN(a.getTime()) ? String(g.taken_from) : a.toLocaleString("id-ID");
}

function statusMark(g) {
  const s = statusOf(g);
  g.status = s;
  if (s === "all") return el("span", { class: "badge badge-keep grp-status", title: "Semua anggota sudah diputuskan" }, "✓");
  if (s === "partial") return el("span", { class: "badge grp-status", title: "Sebagian anggota sudah diputuskan" }, "sebagian");
  return null;
}

function rowOf(g, i) {
  const shown = g.members.slice(0, MAX_MINI);
  const more = g.members.length - shown.length;
  return el("button", {
    type: "button", class: "grp-row" + (i === focus ? " is-active" : ""), role: "option",
    "aria-selected": String(i === focus), "data-id": g.id, tabindex: "-1",
    onclick: pt(() => { if (i !== focus) selectGroup(i); }),
  },
  el("div", { class: "grp-thumbs" },
    ...shown.map((m) => el("img", { class: "grp-thumb" + (m.is_keeper ? " is-keeper" : ""), src: m.thumb || "", alt: "", loading: "lazy", draggable: "false" })),
    ...(more > 0 ? [el("span", { class: "grp-more muted" }, `+${more}`)] : [])),
  el("div", {},
    el("div", { class: "grp-line" }, el("strong", {}, g.summary), statusMark(g)),
    el("div", { class: "muted grp-sub" }, fmtRange(g))));
}

function paintList() {
  listEl.replaceChildren(...data.groups.map(rowOf));
  const act = listEl.querySelector(".is-active");
  if (act && act.scrollIntoView) act.scrollIntoView({ block: "nearest" });
  countEl.textContent = `${fmtNum(data.total)} grup`;
  acceptAllBtn.disabled = !data.groups.length;
  pagerEl.replaceChildren(
    el("button", { type: "button", class: "btn btn-ghost", disabled: page <= 1, onclick: pt(() => goPage(page - 1)) }, "‹ Sebelumnya"),
    el("span", { class: "muted" }, `${data.page} / ${data.pages}`),
    el("button", { type: "button", class: "btn btn-ghost", disabled: page >= data.pages, onclick: pt(() => goPage(page + 1)) }, "Berikutnya ›"));
}

function paintRow(i) {
  const g = data.groups[i];
  const old = listEl.children[i];
  if (g && old) old.replaceWith(rowOf(g, i));
}

async function loadList() {
  data = await api("GET", `/api/review/groups/${encodeURIComponent(category)}?page=${page}`);
  page = data.page;
  if (wantLast) { focus = Math.max(0, data.groups.length - 1); wantLast = false; }
  if (focus >= data.groups.length) focus = Math.max(0, data.groups.length - 1);
  paintList();
  showActive();
}

async function goPage(n, last = false) {
  if (n < 1 || n > data.pages) return;
  page = n;
  focus = 0;
  wantLast = last;
  clearDetail(); // until the new page arrives nothing on screen may be decided
  try { await loadList(); } catch (e) { fail(e); }
}

function selectGroup(i) {
  if (i < 0 || i >= data.groups.length) return;
  const prev = focus;
  focus = i;
  for (const [idx, on] of [[prev, false], [i, true]]) {
    const row = listEl.children[idx];
    if (row) { row.classList.toggle("is-active", on); row.setAttribute("aria-selected", String(on)); }
  }
  const act = listEl.children[i];
  if (act && act.scrollIntoView) act.scrollIntoView({ block: "nearest" });
  showActive();
}

// --- active group: large comparison -------------------------------------------------------

function destroyZoom() {
  if (zoom) { zoom.destroy(); zoom = null; }
}

// Forget the shown group before anything async happens, so a key press while the next group
// loads can never land on the previous group's members (readyGroup refuses instead).
function clearDetail() {
  destroyZoom();
  clearTimeout(detailTimer);
  wantGroup = null;
  detail = null;
  cards = [];
  mfocus = 0;
  cardsEl.replaceChildren(el("p", { class: "empty muted" }, "Memuat…"));
}

// The active group, but only once its detail is the one on screen; otherwise null + a toast.
function readyGroup() {
  const g = data.groups[focus];
  if (g && detail && detail.id === g.id) return g;
  if (g) toast("Masih memuat grup…");
  return null;
}

function showActive() {
  clearDetail();
  const g = data.groups[focus];
  if (!g) {
    metaEl.textContent = "";
    reasonEl.hidden = true;
    actionsEl.replaceChildren();
    cardsEl.replaceChildren(el("p", { class: "empty" }, "Tidak ada grup yang perlu direview di kategori ini."));
    return;
  }
  wantGroup = g.id;
  const cached = detailCache.get(g.id);
  if (cached) { renderDetail(cached); return; }
  detailTimer = setTimeout(async () => {
    try {
      const d = await api("GET", `/api/review/group/${encodeURIComponent(g.id)}`);
      detailCache.set(g.id, d);
      if (detailCache.size > 20) detailCache.delete(detailCache.keys().next().value);
      if (wantGroup === g.id) renderDetail(d);
    } catch (e) { fail(e); }
  }, DETAIL_DELAY_MS);
}

function badgeOf(m) {
  if (m.decision) return el("span", { class: "badge " + (m.decision === "keep" ? "badge-keep" : "badge-danger") }, m.decision === "keep" ? "Keep" : "Delete");
  return el("span", { class: "badge badge-muted" }, m.is_keeper ? "Saran: Keep" : "Saran: Delete");
}

function dateOf(m) {
  if (!m.taken_at) return null;
  const d = new Date(m.taken_at);
  return Number.isNaN(d.getTime()) ? String(m.taken_at) : d.toLocaleString("id-ID");
}

function makeCard(m, i) {
  const p = m.primary || {};
  const media = m.media || {};
  const src = media.kind === "video" ? media.thumb : (media.view || media.thumb);
  const pane = el("div", { class: "cmp-img" },
    el("img", { src: src || "", alt: p.name || "", draggable: "false" }));
  const stateSlot = el("div", { class: "cmp-state" });
  const keepBtn = el("button", { type: "button", class: "btn btn-keep", onclick: pt(() => decide(i, "keep")) }, "Keep ", el("kbd", { class: "kbd" }, "S"));
  const delBtn = el("button", { type: "button", class: "btn btn-danger", onclick: pt(() => decide(i, "discard")) }, "Delete ", el("kbd", { class: "kbd" }, "D"));
  const actions = el("div", { class: "cmp-actions" }, keepBtn, delBtn);
  const facts = [
    p.width && p.height ? `${p.width} × ${p.height}` : null,
    p.size_total != null ? fmtBytes(p.size_total) : null,
    dateOf(m),
  ].filter(Boolean);
  const node = el("article", { class: "cmp-card", "data-id": m.unit_id },
    pane,
    el("div", { class: "cmp-body" },
      stateSlot,
      el("div", { class: "cmp-name", title: p.name || "" }, p.name || "-"),
      el("div", { class: "muted cmp-facts" }, facts.join(" · ") || "-"),
      el("div", { class: "muted cmp-facts" }, [p.source_label, LOCATION_LABELS[m.location] || m.location].filter(Boolean).join(" · ")),
      m.origin ? el("div", { class: "cmp-origin" }, m.origin) : null,
      actions));
  pane.addEventListener("click", () => { if (i !== mfocus) setMemberFocus(i); });
  function paint() {
    node.classList.toggle("is-focused", i === mfocus);
    node.classList.toggle("is-keep", m.decision === "keep");
    node.classList.toggle("is-discard", m.decision === "discard");
    const lock = locked(m);
    stateSlot.replaceChildren(...[badgeOf(m),
      lock ? el("span", { class: "badge badge-muted" }, m.location === "trash" ? "Sudah di _sampah" : "Sudah diproses") : null].filter(Boolean));
    actions.hidden = lock;
    keepBtn.setAttribute("aria-pressed", String(m.decision === "keep"));
    delBtn.setAttribute("aria-pressed", String(m.decision === "discard"));
  }
  paint();
  return { m, node, pane, paint };
}

function renderDetail(d) {
  destroyZoom();
  detail = d;
  const first = d.members.findIndex((m) => !m.is_keeper && !locked(m));
  mfocus = first >= 0 ? first : 0;
  cards = d.members.map(makeCard);
  cardsEl.replaceChildren(...cards.map((c) => c.node));
  metaEl.textContent = `${d.summary} · ${d.folder || "Folder tidak diketahui"} · ${fmtRange(d)}`;
  reasonEl.hidden = !d.reason;
  reasonEl.replaceChildren(el("strong", {}, "Alasan saran: "), d.reason || "");
  actionsEl.replaceChildren(
    el("button", { type: "button", class: "btn btn-primary", onclick: pt(() => acceptActive()) }, "Ikuti saran ", el("kbd", { class: "kbd" }, "↵")),
    el("button", { type: "button", class: "btn btn-secondary", onclick: pt(() => keepAll()) }, "Keep semua"),
    el("button", { type: "button", class: "btn btn-secondary", onclick: pt(() => toggleZoom()) }, "Zoom bareng ", el("kbd", { class: "kbd" }, "Z")),
    el("button", { type: "button", class: "btn btn-ghost", onclick: pt(() => openFull()) }, "Layar penuh ", el("kbd", { class: "kbd" }, "Spasi")));
}

function setMemberFocus(i) {
  if (i < 0 || i >= cards.length) return;
  mfocus = i;
  cards.forEach((c) => c.paint());
  const node = cards[i].node;
  if (node.scrollIntoView) node.scrollIntoView({ block: "nearest", inline: "nearest" });
}

function toggleZoom() {
  if (zoom) { destroyZoom(); return; }
  if (!readyGroup()) return;
  const panes = cards.filter((c) => (c.m.media || {}).kind !== "video").map((c) => c.pane);
  if (!panes.length) { toast("Tidak ada foto untuk di-zoom."); return; }
  zoom = createZoom(panes);
  toast("Zoom bareng aktif: scroll untuk zoom, seret untuk geser, klik ganda untuk reset.");
}

// --- decisions ---------------------------------------------------------------------------

function afterDecision() {
  bar.refresh();
  reviewNav.refresh();
}

// Store a saved decision by id: the list and the detail may have been replaced while the
// request was in flight, so never write into objects captured before the await.
function record(gid, uid, decision) {
  const details = new Set([detailCache.get(gid), detail && detail.id === gid ? detail : null]);
  for (const d of details) {
    const m = d && d.members.find((x) => x.unit_id === uid);
    if (m) m.decision = decision;
  }
  const idx = data.groups.findIndex((x) => x.id === gid);
  if (idx < 0) return; // group no longer listed: the server has it, nothing to paint
  const bm = data.groups[idx].members.find((x) => x.unit_id === uid);
  if (bm) bm.decision = decision;
  paintRow(idx);
  if (idx === focus && detail && detail.id === gid) cards.forEach((c) => c.paint());
}

async function decide(i, decision) {
  const g = readyGroup();
  if (!g) return false;
  const c = cards[i];
  if (!c || !detail.members.includes(c.m)) return false;
  if (locked(c.m)) { toast("Anggota ini sudah diproses, tidak bisa diubah."); return false; }
  if (bar.applying) { toast("Apply sedang berjalan, tunggu sampai selesai."); return false; }
  if (busy) { toast("Masih menyimpan keputusan sebelumnya, coba lagi sebentar."); return false; }
  const gid = g.id;
  const uid = c.m.unit_id;
  busy = true;
  try {
    await api("POST", "/api/review/decision", { unit_id: uid, decision });
    record(gid, uid, decision);
    afterDecision();
    return true;
  } catch (e) { fail(e); return false; } finally { busy = false; }
}

async function keepAll() {
  if (busy) return;
  const g = readyGroup();
  if (!g) return;
  if (bar.applying) { toast("Apply sedang berjalan, tunggu sampai selesai."); return; }
  const gid = g.id;
  const uids = cards.filter((c) => !locked(c.m) && c.m.decision !== "keep").map((c) => c.m.unit_id);
  busy = true;
  try {
    for (const uid of uids) {
      await api("POST", "/api/review/decision", { unit_id: uid, decision: "keep" });
      record(gid, uid, "keep");
    }
  } catch (e) { fail(e); } finally { busy = false; afterDecision(); }
}

async function acceptActive() {
  const g = data.groups[focus];
  if (!g || busy) return;
  if (bar.applying) { toast("Apply sedang berjalan, tunggu sampai selesai."); return; }
  busy = true;
  try {
    // The server keeps explicit unapplied decisions, so re-read the page instead of painting
    // the suggestion locally.
    await api("POST", "/api/review/accept", { group_ids: [g.id] });
    detailCache.delete(g.id);
    const at = data.groups.findIndex((x) => x.id === g.id); // the list may have moved meanwhile
    if (at >= 0) focus = at;
    if (focus < data.groups.length - 1) focus += 1;
    else if (page < data.pages) { page += 1; focus = 0; }
    else toast("Itu grup terakhir di kategori ini.");
    await loadList();
    bar.refresh();
    reviewNav.refresh();
  } catch (e) { fail(e); } finally { busy = false; }
}

async function acceptAll() {
  if (!data.groups.length || busy) return;
  if (bar.applying) { toast("Apply sedang berjalan, tunggu sampai selesai."); return; }
  const ok = await confirmDialog({
    title: "Ikuti saran semua di halaman ini?",
    body: [`${data.groups.length} grup di halaman ini akan mengikuti saran untuk anggota yang belum diputuskan.`,
      "Keputusan yang sudah Anda buat tidak diubah. Belum ada file yang dipindah sebelum Apply."],
    okLabel: "Ikuti saran semua",
  });
  if (!ok) return;
  busy = true;
  try {
    await api("POST", "/api/review/accept", { group_ids: data.groups.map((g) => g.id) });
    detailCache.clear();
    await loadList();
    bar.refresh();
    reviewNav.refresh();
  } catch (e) { fail(e); } finally { busy = false; }
}

// --- fullscreen compare ------------------------------------------------------------------

function openFull() {
  if (viewerOpen || !readyGroup()) return;
  const d = detail;
  const members = d.members;
  const keeper = members.find((m) => m.is_keeper) || members[0];
  const others = members.filter((m) => m !== keeper);
  if (!others.length) return;
  const start = Math.max(0, others.indexOf(members[mfocus]));
  viewerOpen = true;
  openViewer({
    mode: "compare",
    items: others.map((m) => [keeper, m]),
    index: start,
    onDecide: async (item, decision) => {
      if (detail !== d) { toast("Masih memuat grup…"); return false; }
      const i = members.indexOf(item);
      if (i < 0) return false;
      return decide(i, decision === "suggest" ? (item.is_keeper ? "keep" : "discard") : decision);
    },
    onClose: () => {
      viewerOpen = false;
      cards.forEach((c) => c.paint());
    },
  });
}

// --- keys ----------------------------------------------------------------------------------

function moveGroup(d) {
  if (busy) return;
  const n = focus + d;
  if (n >= 0 && n < data.groups.length) { selectGroup(n); return; }
  if (d > 0 && page < data.pages) goPage(page + 1);
  else if (d < 0 && page > 1) goPage(page - 1, true);
}

const keyMap = {
  ArrowUp: () => moveGroup(-1),
  ArrowDown: () => moveGroup(1),
  ArrowLeft: () => setMemberFocus(mfocus - 1),
  ArrowRight: () => setMemberFocus(mfocus + 1),
  s: () => decide(mfocus, "keep"),
  d: () => decide(mfocus, "discard"),
  Enter: () => acceptActive(),
  " ": () => openFull(),
  z: () => toggleZoom(),
};
let unbindKeys = bindKeys(keyMap);
window.addEventListener("pagehide", () => { unbindKeys(); unbindKeys = () => {}; });
window.addEventListener("pageshow", (ev) => { if (ev.persisted) unbindKeys = bindKeys(keyMap); });

registerHelp([
  { title: "Review grup", keys: [["↑ ↓", "Grup sebelumnya / berikutnya (pindah halaman otomatis di batas)"],
    ["← →", "Pindah antar anggota grup"], ["S", "Keep anggota terpilih"], ["D", "Delete anggota terpilih"],
    ["Enter", "Ikuti saran untuk grup ini, lalu ke grup berikutnya"], ["Spasi", "Layar penuh (banding dengan yang disarankan Keep)"],
    ["Z", "Zoom bareng: semua foto di grup di-zoom dan digeser serentak"], ["?", "Bantuan ini"]] },
  { title: "Penanda", keys: [["✓", "Semua anggota grup sudah diputuskan"], ["sebagian", "Baru sebagian anggota yang diputuskan"]] },
]);

// --- start ---------------------------------------------------------------------------------

loadList().catch(fail);
