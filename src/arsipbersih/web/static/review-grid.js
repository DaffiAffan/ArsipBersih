import { api, el } from "/static/api.js";
import { bindKeys } from "/static/keys.js";
import { openViewer } from "/static/viewer.js";
import { watchJob } from "/static/job.js";

const main = document.getElementById("rv-main");
const root = document.getElementById("grid-root");
const category = main.dataset.category;
const label = main.dataset.label || category;

const SEEN_CHUNK = 1000;
const SEEN_DELAY_MS = 1000;

let items = [];
let total = 0;
let sort = "score";
let focus = 0;
let pendingApply = 0;
let message = "";
let applying = false;
let busy = false;
let loading = false;
let exhausted = false;
let generation = 0;
let viewerState = null; // { vitems, loaded:Set, handle }

// DOM pieces (built once in buildShell)
let gridEl = null;
let sentinel = null;
let applyBtn = null;
let msgEl = null;
let countEl = null;
let cellObserver = null;
let moreObserver = null;
const cells = new Map(); // unit_id -> element

function setMessage(text) {
  message = text || "";
  if (msgEl) { msgEl.textContent = message; msgEl.hidden = !message; }
}

function fail(err) { setMessage(err && err.message ? err.message : String(err)); }

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
    updateHead();
  } catch (_) { /* keep the previous numbers */ }
}

function updateHead() {
  if (applyBtn) {
    applyBtn.textContent = `Terapkan (${pendingApply})`;
    applyBtn.disabled = pendingApply === 0 || applying;
  }
  if (countEl) countEl.textContent = `${items.length} dari ${total} item`;
}

// --- seen tracking --------------------------------------------------------------------

const seenQueue = new Set();
let seenTimer = null;
let seenSending = false;

function queueSeen(item) {
  if (!item || item.seen || seenQueue.has(item.unit_id)) return;
  seenQueue.add(item.unit_id);
  if (!seenTimer) seenTimer = setTimeout(() => { seenTimer = null; flushSeen(); }, SEEN_DELAY_MS);
}

async function flushSeen() {
  if (seenTimer) { clearTimeout(seenTimer); seenTimer = null; }
  if (seenSending) { // another flush is in flight; try again once it settles
    seenTimer = setTimeout(() => { seenTimer = null; flushSeen(); }, SEEN_DELAY_MS);
    return;
  }
  if (!seenQueue.size) return;
  seenSending = true;
  const ids = [...seenQueue];
  seenQueue.clear();
  try {
    for (let i = 0; i < ids.length; i += SEEN_CHUNK) {
      const part = ids.slice(i, i + SEEN_CHUNK);
      try {
        await api("POST", "/api/review/seen", { unit_ids: part });
        const set = new Set(part);
        for (const it of items) if (set.has(it.unit_id)) it.seen = true;
      } catch (_) {
        for (const id of part) seenQueue.add(id); // retry on the next flush
      }
    }
  } finally { seenSending = false; }
  if (seenQueue.size && !seenTimer) seenTimer = setTimeout(() => { seenTimer = null; flushSeen(); }, SEEN_DELAY_MS);
  refreshChrome();
}

function flushSeenOnLeave() {
  const ids = [...seenQueue];
  seenQueue.clear();
  for (let i = 0; i < ids.length; i += SEEN_CHUNK) {
    try {
      fetch("/api/review/seen", {
        method: "POST",
        keepalive: true,
        headers: { "X-ArsipBersih": "1", "Content-Type": "application/json" },
        body: JSON.stringify({ unit_ids: ids.slice(i, i + SEEN_CHUNK) }),
      }).catch(() => {});
    } catch (_) { /* leaving anyway */ }
  }
}

window.addEventListener("pagehide", flushSeenOnLeave);

function observeSeen(cell, item) {
  if (item.seen || !cellObserver) return;
  cell.__item = item;
  cellObserver.observe(cell);
}

function makeCellObserver() {
  if (typeof IntersectionObserver === "undefined") return null;
  return new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!e.isIntersecting) continue;
      queueSeen(e.target.__item);
      cellObserver.unobserve(e.target);
    }
  }, { threshold: 0.5 });
}

// --- grid rendering -------------------------------------------------------------------

function fmtScore(v) {
  const n = Number(v);
  return (Math.abs(n) >= 10 ? n.toFixed(1) : n.toFixed(2)).replace(".", ",");
}

function indexOfUnit(id) { return items.findIndex((x) => x.unit_id === id); }

function paintCell(cell, item, i) {
  cell.className = "gcell" + (item.decision === "keep" ? " keep" : item.decision === "discard" ? " discard" : "")
    + (i === focus ? " focused" : "");
  const mark = cell.querySelector(".gmark");
  if (mark) {
    mark.textContent = item.decision === "keep" ? "✓ Diselamatkan" : item.decision === "discard" ? "Buang" : "";
    mark.hidden = !item.decision;
  }
}

function makeCell(item, i) {
  const title = item.reason ? String(item.reason) : "";
  const cell = el("div", { class: "gcell", tabindex: "-1", "data-id": item.unit_id, title },
    el("img", { src: item.thumb || "", alt: "", loading: "lazy", draggable: "false" }),
    item.score != null ? el("span", { class: "score" }, fmtScore(item.score)) : null,
    el("span", { class: "gmark", hidden: true }));
  let clickTimer = null; // delay single-click so a double-click does not toggle first
  cell.addEventListener("click", () => {
    clearTimeout(clickTimer);
    clickTimer = setTimeout(() => {
      setFocus(indexOfUnit(item.unit_id), false);
      toggleKeep(item.unit_id);
    }, 250);
  });
  cell.addEventListener("dblclick", () => {
    clearTimeout(clickTimer);
    openFull(indexOfUnit(item.unit_id));
  });
  paintCell(cell, item, i);
  cells.set(item.unit_id, cell);
  observeSeen(cell, item);
  return cell;
}

function repaint(item) {
  const i = indexOfUnit(item.unit_id);
  const cell = cells.get(item.unit_id);
  if (cell && i >= 0) paintCell(cell, item, i);
}

function setFocus(i, scroll = true) {
  if (i < 0 || i >= items.length) return;
  const prev = items[focus];
  focus = i;
  if (prev) repaint(prev);
  repaint(items[focus]);
  const cell = cells.get(items[focus].unit_id);
  if (scroll && cell && cell.scrollIntoView) cell.scrollIntoView({ block: "nearest" });
  if (i >= items.length - 12) loadMore();
}

function buildShell() {
  if (cellObserver) cellObserver.disconnect();
  if (moreObserver) moreObserver.disconnect();
  cells.clear();
  cellObserver = makeCellObserver();
  applyBtn = el("button", { type: "button", class: "primary", disabled: true, onclick: openApply }, "Terapkan (0)");
  const sortSel = el("select", { "aria-label": "Urutan", onchange: (e) => { sort = e.target.value; reload(); } },
    el("option", { value: "score" }, "Skor (terburuk dulu)"),
    el("option", { value: "date" }, "Tanggal"));
  sortSel.value = sort;
  countEl = el("span", { class: "muted" });
  msgEl = el("div", { class: "vw-error", hidden: true });
  gridEl = el("div", { class: "gr-grid" });
  sentinel = el("div", { class: "gr-more" });
  root.replaceChildren(
    el("div", { class: "rv-head" }, el("h1", {}, label), countEl, sortSel, applyBtn),
    el("div", { class: "rv-hint" },
      "Semua dianggap dibuang. Klik thumbnail untuk menyelamatkan · ← → pindah · K selamatkan · D buang · Enter terima saran · Spasi atau klik ganda layar penuh"),
    msgEl, gridEl, sentinel);
  if (typeof IntersectionObserver !== "undefined") {
    moreObserver = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) loadMore();
    }, { rootMargin: "600px" });
    moreObserver.observe(sentinel);
  }
  setMessage(message);
  updateHead();
}

function renderEmpty() {
  sentinel.textContent = items.length ? "" : "Tidak ada item di kategori ini.";
}

async function loadMore() {
  if (loading || exhausted) return;
  loading = true;
  const gen = generation;
  try {
    const data = await api("GET",
      `/api/review/grid/${encodeURIComponent(category)}?sort=${sort}&offset=${items.length}`);
    if (gen !== generation) return;
    total = data.total;
    const fresh = data.items;
    if (!fresh.length || items.length + fresh.length >= total) exhausted = true;
    const base = items.length;
    items.push(...fresh);
    fresh.forEach((it, n) => gridEl.append(makeCell(it, base + n)));
    if (viewerState) {
      for (const it of fresh) viewerState.vitems.push(placeholder(it));
    }
    sentinel.textContent = exhausted ? "" : "Memuat…";
    renderEmpty();
    updateHead();
  } catch (e) { if (gen === generation) { exhausted = true; fail(e); } } finally { loading = false; }
  // keep filling if the sentinel is still on screen (observer will not re-fire by itself)
  if (gen === generation && !exhausted && sentinel && sentinel.getBoundingClientRect
      && sentinel.getBoundingClientRect().top < window.innerHeight + 600) loadMore();
}

async function reload() {
  generation += 1;
  items = [];
  total = 0;
  focus = 0;
  exhausted = false;
  loading = false;
  seenQueue.clear(); // ids not yet sent stay unseen; they will be re-observed
  buildShell();
  await refreshChrome();
  await loadMore();
  if (items.length) setFocus(0, false);
}

// --- decisions ------------------------------------------------------------------------

async function decide(unitId, decision) {
  if (busy) return false;
  const item = items[indexOfUnit(unitId)];
  if (!item) return false;
  busy = true;
  try {
    await api("POST", "/api/review/decision", { unit_id: unitId, decision });
    item.decision = decision;
    repaint(item);
    setMessage("");
    refreshChrome();
    return true;
  } catch (e) { fail(e); return false; } finally { busy = false; }
}

function toggleKeep(unitId) {
  const item = items[indexOfUnit(unitId)];
  if (!item) return Promise.resolve(false);
  queueSeen(item);
  return decide(unitId, item.decision === "keep" ? null : "keep");
}

function focusedItem() { return items[focus]; }

async function acceptFocused() {
  const it = focusedItem();
  if (!it) return;
  queueSeen(it);
  const ok = await decide(it.unit_id, null);
  if (ok && focus < items.length - 1) setFocus(focus + 1);
}

const unbindGrid = bindKeys({
  ArrowLeft: () => setFocus(focus - 1),
  ArrowRight: () => setFocus(focus + 1),
  k: () => { const it = focusedItem(); if (it) { queueSeen(it); decide(it.unit_id, "keep"); } },
  d: () => { const it = focusedItem(); if (it) { queueSeen(it); decide(it.unit_id, "discard"); } },
  Enter: acceptFocused,
  " ": () => openFull(focus),
});
window.addEventListener("pagehide", unbindGrid);

// --- fullscreen viewer ----------------------------------------------------------------

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
      if (applying) return;
      const idx = indexOfUnit(card.unit_id);
      if (decision === "suggest") await decide(card.unit_id, null);
      else await decide(card.unit_id, decision);
      const it = items[idx];
      if (it && viewerState === vs) {
        card.decision = it.decision;
        const vi = vs.vitems.findIndex((c) => c.unit_id === card.unit_id);
        if (vi >= 0) vs.vitems[vi].decision = it.decision;
        if (vs.handle && vs.handle.index === vi) vs.handle.go(vi);
      }
    },
    onIndex: (n) => {
      setFocus(n);
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
      flushSeen();
    },
  });
  ensureCard(i + 1);
  ensureCard(i - 1);
  if (i >= items.length - 6) loadMore();
}

// --- apply dialog ---------------------------------------------------------------------

function modal(title, ...body) {
  const overlay = el("div", { class: "rv-modal", role: "dialog", "aria-modal": "true" },
    el("div", { class: "rv-dialog" }, el("h2", {}, title), ...body));
  document.body.append(overlay);
  return overlay;
}

async function openApply() {
  if (applying) return;
  await flushSeen(); // so "belum dilihat" reflects what is really on screen
  let preview;
  try { preview = await api("GET", `/api/review/apply/${encodeURIComponent(category)}`); } catch (e) { fail(e); return; }
  const unbind = bindKeys({ Escape: () => { if (!applying) close(); } });
  const confirm = el("button", { type: "button", class: "primary", disabled: preview.keep + preview.discard === 0 }, "Terapkan");
  const cancel = el("button", { type: "button" }, "Batal");
  const actions = el("div", { class: "actions" }, cancel, confirm);
  const box = modal(`Terapkan: ${label}`, el("p", {}, preview.text), actions);
  function close() { unbind(); box.remove(); }
  cancel.addEventListener("click", close);
  confirm.addEventListener("click", async () => {
    if (applying) return;
    applying = true;
    updateHead();
    confirm.disabled = true;
    cancel.disabled = true;
    let started;
    try { started = await api("POST", `/api/review/apply/${encodeURIComponent(category)}`); } catch (e) {
      applying = false;
      updateHead();
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
      reload().catch(fail);
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

reload().catch(fail);
