import { api, el, fmtBytes } from "/static/api.js";
import { bindKeys } from "/static/keys.js";
import { openViewer } from "/static/viewer.js";
import { watchJob } from "/static/job.js";

const main = document.getElementById("rv-main");
const root = document.getElementById("group-root");
const category = main.dataset.category;
const label = main.dataset.label || category;

let page = 1;
let data = { groups: [], page: 1, pages: 1, total: 0 };
let focus = 0;
let pendingApply = 0;
let compare = null; // { index, group, focus, unbind }
let message = "";
let applying = false;
let busy = false;

const DECISION_LABEL = { keep: "Keep", discard: "Delete" };

function fail(err) {
  message = err && err.message ? err.message : String(err);
  renderList();
}

// --- sidebar & header counts ----------------------------------------------------------

async function refreshChrome() {
  try {
    const cats = await api("GET", "/api/review/categories");
    const byCat = new Map(cats.map((c) => [c.category, c]));
    for (const a of document.querySelectorAll("#rv-cats a[data-cat]")) {
      const c = byCat.get(a.dataset.cat);
      a.hidden = !c && a.dataset.cat !== category;
      const badge = a.querySelector(".rv-count");
      if (badge) badge.textContent = c ? String(c.count) : "0";
    }
    const mine = byCat.get(category);
    pendingApply = mine ? mine.pending_apply : 0;
  } catch (_) { /* keep the previous numbers */ }
}

// --- list mode ------------------------------------------------------------------------

async function loadList() {
  data = await api("GET", `/api/review/groups/${encodeURIComponent(category)}?page=${page}`);
  page = data.page;
  if (focus >= data.groups.length) focus = Math.max(0, data.groups.length - 1);
  await refreshChrome();
  renderList();
}

// Owner decision (strong colour) replaces the suggestion badge. Photos are never dimmed.
function stateBadge(m) {
  if (m.decision) return el("span", { class: "badge decided " + m.decision }, DECISION_LABEL[m.decision] || m.decision);
  return m.is_keeper
    ? el("span", { class: "badge suggest keep" }, "✓ Saran: Keep")
    : el("span", { class: "badge suggest discard" }, "Saran: Delete");
}

function memThumb(m) {
  const box = el("div", { class: "mem" },
    el("img", { src: m.thumb || "", alt: "", loading: "lazy", draggable: "false" }));
  box.append(stateBadge(m));
  return box;
}

function fmtRange(g) {
  if (!g.taken_from) return "Tanggal tidak diketahui";
  const a = new Date(g.taken_from);
  return isNaN(a) ? String(g.taken_from) : a.toLocaleString("id-ID");
}

function groupRow(g, i) {
  const row = el("div", {
    class: "grp" + (i === focus ? " focused" : ""),
    tabindex: "-1",
    "data-id": g.id,
    onclick: () => { focus = i; openCompare(i); },
  },
  el("div", { class: "grp-thumbs" }, g.members.map(memThumb)),
  el("div", { class: "grp-info" },
    el("strong", {}, g.folder || "Folder tidak diketahui"),
    el("div", {}, fmtRange(g)),
    el("div", {}, g.summary)));
  return row;
}

function renderList() {
  if (compare) return;
  const applyBtn = el("button", { type: "button", class: "primary", disabled: pendingApply === 0 || applying,
    onclick: openApply }, `Apply (${pendingApply})`);
  const head = el("div", { class: "rv-head" }, el("h1", {}, label), applyBtn);
  const children = [head];
  if (message) children.push(el("div", { class: "vw-error" }, message));
  if (!data.groups.length) {
    children.push(el("p", { class: "muted" }, "Tidak ada grup yang perlu direview di kategori ini."));
  } else {
    children.push(el("div", { class: "rv-hint" },
      "← → pindah grup · Enter ikuti saran · Spasi/klik: bandingkan"));
    children.push(el("button", { type: "button", onclick: acceptAll },
      `Ikuti saran semua di halaman ini (${data.groups.length} grup)`));
    children.push(el("div", { style: "margin-top:10px" }, data.groups.map(groupRow)));
    children.push(el("div", { class: "rv-pager" },
      el("button", { type: "button", disabled: page <= 1, onclick: () => goPage(page - 1) }, "Sebelumnya"),
      el("span", {}, `Halaman ${data.page} dari ${data.pages} · ${data.total} grup`),
      el("button", { type: "button", disabled: page >= data.pages, onclick: () => goPage(page + 1) }, "Berikutnya")));
  }
  root.replaceChildren(...children);
  const f = root.querySelector(".grp.focused");
  if (f && f.scrollIntoView) f.scrollIntoView({ block: "nearest" });
}

async function goPage(n) {
  page = n;
  focus = 0;
  message = "";
  try { await loadList(); } catch (e) { fail(e); }
}

async function acceptAll() {
  try {
    await api("POST", "/api/review/accept", { group_ids: data.groups.map((g) => g.id) });
    message = "";
    await loadList();
  } catch (e) { fail(e); }
}

async function acceptFocused() {
  const g = data.groups[focus];
  if (!g || busy) return;
  busy = true;
  try {
    // The server keeps explicit unapplied decisions, so re-read the page instead of
    // painting the suggestion locally.
    await api("POST", "/api/review/accept", { group_ids: [g.id] });
    message = "";
    if (focus < data.groups.length - 1) focus += 1;
    await loadList();
  } catch (e) { fail(e); } finally { busy = false; }
}

const unbindList = bindKeys({
  ArrowLeft: () => { if (!busy && focus > 0) { focus -= 1; renderList(); } },
  ArrowRight: () => { if (!busy && focus < data.groups.length - 1) { focus += 1; renderList(); } },
  Enter: acceptFocused,
  " ": () => { if (!busy && data.groups[focus]) openCompare(focus); },
});
window.addEventListener("pagehide", unbindList);

// --- compare mode ---------------------------------------------------------------------

async function openCompare(index) {
  const g = data.groups[index];
  if (!g) { closeCompare(); return; }
  busy = true;
  try {
    const detail = await api("GET", `/api/review/group/${encodeURIComponent(g.id)}`);
    if (!compare) {
      compare = { unbind: bindKeys({
        ArrowLeft: () => moveCompareFocus(-1),
        ArrowRight: () => moveCompareFocus(1),
        s: () => decideFocused("keep"),
        d: () => decideFocused("discard"),
        Enter: acceptCompare,
        Escape: closeCompare,
      }) };
    }
    compare.index = index;
    compare.group = detail;
    compare.focus = Math.max(0, detail.members.findIndex((m) => !m.is_keeper));
    renderCompare();
  } catch (e) { busy = false; await closeCompare(); fail(e); return; }
  busy = false;
}

function infoRows(m) {
  const p = m.primary || {};
  const date = m.taken_at ? (isNaN(new Date(m.taken_at)) ? m.taken_at : new Date(m.taken_at).toLocaleString("id-ID")) : "Tidak diketahui";
  const rows = [
    ["Nama file", p.name || "-"],
    ["Resolusi", p.width && p.height ? `${p.width} × ${p.height}` : "-"],
    ["Ukuran", fmtBytes(p.size_total)],
    ["Sumber", p.source_label || "-"],
    ["Tanggal", date],
    ["GPS", p.has_gps ? "ada" : "tidak ada"],
    ["Skor tajam", p.sharpness != null ? Number(p.sharpness).toFixed(1).replace(".", ",") : "-"],
  ];
  return el("dl", { class: "vw-info" },
    rows.map(([k, v]) => el("div", { class: "vw-row" }, el("dt", {}, k), el("dd", {}, v))));
}

function renderCompare() {
  if (!compare) return;
  const g = compare.group;
  const cards = g.members.map((m, i) => {
    const media = m.media || {};
    const src = media.kind === "video" ? media.thumb : (media.view || media.thumb);
    return el("div", { class: "cmp-card" + (i === compare.focus ? " focused" : "") },
      el("div", { class: "cmp-img" },
        el("img", { class: "big", src: src || "", alt: "", draggable: "false",
          onclick: () => { compare.focus = i; openCompareViewer(i); } }),
        stateBadge(m)),
      infoRows(m),
      el("div", { class: "cmp-actions" },
        el("button", { type: "button", class: m.decision === "keep" ? "on" : "", onclick: () => decide(i, "keep") }, "Keep"),
        el("button", { type: "button", class: m.decision === "discard" ? "on" : "", onclick: () => decide(i, "discard") }, "Delete")));
  });
  const head = el("div", { class: "rv-head" },
    el("h1", {}, `${label}: grup ${compare.index + 1} dari ${data.groups.length}`),
    el("button", { type: "button", onclick: closeCompare }, "Kembali ke daftar"),
    el("button", { type: "button", class: "primary", onclick: acceptCompare }, "Ikuti saran"));
  const children = [head,
    el("div", { class: "rv-hint" }, `${g.summary} · ← → pindah · S keep · D delete · Enter ikuti saran · Esc kembali · klik foto untuk layar penuh`),
    el("div", { class: "cmp-grid" }, cards)];
  if (message) children.splice(1, 0, el("div", { class: "vw-error" }, message));
  root.replaceChildren(...children);
}

function moveCompareFocus(d) {
  if (!compare) return;
  const n = compare.focus + d;
  if (n < 0 || n >= compare.group.members.length) return;
  compare.focus = n;
  renderCompare();
}

async function decide(i, decision) {
  const m = compare && compare.group.members[i];
  if (!m || busy) return;
  busy = true;
  try {
    await api("POST", "/api/review/decision", { unit_id: m.unit_id, decision });
    m.decision = decision;
    message = "";
    renderCompare();
    refreshChrome();
  } catch (e) { message = e.message; renderCompare(); } finally { busy = false; }
}

function decideFocused(decision) {
  if (compare) decide(compare.focus, decision);
}

async function acceptCompare() {
  if (!compare || busy) return;
  busy = true;
  const g = compare.group;
  const next = compare.index + 1;
  try {
    await api("POST", "/api/review/accept", { group_ids: [g.id] });
    message = "";
    refreshChrome(); // the next group / the list are re-fetched from the server below
    busy = false;
    if (next < data.groups.length) await openCompare(next);
    else await closeCompare();
  } catch (e) { message = e.message; renderCompare(); } finally { busy = false; }
}

async function closeCompare() {
  if (!compare) return;
  const keepFocus = compare.index;
  compare.unbind();
  compare = null;
  focus = keepFocus;
  try { await loadList(); } catch (e) { fail(e); }
}

function openCompareViewer(clicked) {
  const g = compare.group;
  const keeper = g.members.find((m) => m.is_keeper) || g.members[0];
  const others = g.members.filter((m) => m !== keeper);
  if (!others.length) return;
  const pairs = others.map((m) => [keeper, m]);
  const start = Math.max(0, others.indexOf(g.members[clicked]));
  openViewer({
    mode: "compare",
    items: pairs,
    index: start,
    onDecide: async (item, decision) => {
      try {
        if (decision === "suggest") {
          const sug = item.is_keeper ? "keep" : "discard";
          await api("POST", "/api/review/decision", { unit_id: item.unit_id, decision: sug });
          item.decision = sug;
        } else {
          await api("POST", "/api/review/decision", { unit_id: item.unit_id, decision });
          item.decision = decision;
        }
        refreshChrome();
      } catch (e) { message = e.message; }
    },
    onClose: () => renderCompare(),
  });
}

// --- apply dialog ---------------------------------------------------------------------

function modal(title, ...body) {
  const overlay = el("div", { class: "rv-modal", role: "dialog", "aria-modal": "true" },
    el("div", { class: "rv-dialog" }, el("h2", {}, title), ...body));
  document.body.append(overlay);
  return overlay;
}

let applyOpen = false;

async function openApply() {
  if (applying || applyOpen) return;
  applyOpen = true;
  // Key layer first: while the dialog loads or is open, list keys stay inert.
  let closeDialog = null;
  const unbind = bindKeys({ Escape: () => { if (!applying && closeDialog) closeDialog(); } });
  let preview;
  try { preview = await api("GET", `/api/review/apply/${encodeURIComponent(category)}`); } catch (e) {
    unbind(); applyOpen = false; fail(e); return;
  }
  const confirm = el("button", { type: "button", class: "primary", disabled: preview.keep + preview.discard === 0 }, "Apply");
  const cancel = el("button", { type: "button" }, "Batal");
  const actions = el("div", { class: "actions" }, cancel, confirm);
  const box = modal(`Apply: ${label}`, el("p", {}, preview.text), el("p", { class: "muted" }, "File yang di-delete masuk _sampah dan masih bisa di-undo di Riwayat."), actions);
  function close() { unbind(); box.remove(); applyOpen = false; }
  closeDialog = close;
  cancel.addEventListener("click", close);
  confirm.addEventListener("click", async () => {
    applying = true;
    confirm.disabled = true;
    cancel.disabled = true;
    let started;
    try { started = await api("POST", `/api/review/apply/${encodeURIComponent(category)}`, { keep: preview.keep, discard: preview.discard }); } catch (e) {
      applying = false;
      actions.before(el("p", { class: "vw-error" }, e.message));
      cancel.disabled = false;
      return;
    }
    actions.before(el("p", { class: "muted" }, "Memindahkan file…"));
    let stop = () => {};
    let finished = false;
    stop = watchJob((st) => {
      if (finished || st.id !== started.id || st.status === "running" || st.status === "idle") return;
      finished = true;
      setTimeout(() => stop(), 0);
      applying = false;
      if (box.isConnected) showApplyResult(st, close, box);
      loadList().catch(fail);
    });
  });
}

function count(v) { return Array.isArray(v) ? v.length : Number(v) || 0; }

function showApplyResult(st, close, overlay) {
  const box = overlay.querySelector(".rv-dialog");
  const done = el("button", { type: "button", class: "primary", onclick: close }, "Tutup");
  const parts = [el("h2", {}, "Hasil")];
  if (st.status === "done" && st.result) {
    const r = st.result;
    parts.push(el("p", {}, `${count(r.moved_units)} dipindah · ${count(r.failed_units)} gagal · ${count(r.skipped_units)} dilewati`));
    if (r.errors && r.errors.length) {
      parts.push(el("details", {}, el("summary", {}, `Detail (${r.errors.length})`), el("pre", {}, r.errors.join("\n"))));
    }
  } else {
    parts.push(el("p", { class: "vw-error" }, `Gagal: ${st.error || st.status}`));
  }
  parts.push(el("div", { class: "actions" }, done));
  box.replaceChildren(...parts);
}

// --- start ----------------------------------------------------------------------------

loadList().catch(fail);
