// Compact group list on the left of the group review: one row per group with tiny member
// thumbnails, the date and the status ("✓ diputuskan" / "sebagian" / nothing). Groups come in
// pages of 20 (GROUP_PAGE on the server); "Muat 20 grup berikutnya" appends the next page.
import { api, el } from "/static/core/api.js";

const MAX_MINI = 4;
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"];

// Same rule as the server: an archived suggested keeper and trashed members need no decision.
export function statusOf(g) {
  const needing = g.members.filter((m) => m.location !== "trash"
    && !(m.location === "archive" && m.unit_id === g.keeper_id));
  if (!needing.length) return "all";
  const n = needing.filter((m) => m.decision != null).length;
  return n === 0 ? "none" : n === needing.length ? "all" : "partial";
}

export function fmtDate(iso) {
  if (!iso) return "Tanggal tidak diketahui";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso) : `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
}

function statusEl(g) {
  const s = statusOf(g);
  if (s === "all") return el("span", { class: "glist-status is-done" }, "✓ diputuskan");
  if (s === "partial") return el("span", { class: "glist-status" }, "sebagian");
  return null;
}

// onSelect(groupId): a row was chosen (click or select()); onLoadedPage(groupIds): the loaded
// groups changed (every id loaded so far, in list order).
export function mountGroupList(container, { kind, onSelect, onLoadedPage }) {
  const rowsEl = el("div", { class: "glist-rows", role: "listbox", "aria-label": "Daftar grup" });
  const moreBtn = el("button", { type: "button", class: "btn btn-ghost glist-more", hidden: true,
    onclick: () => { loadMore().catch(() => {}); } }, "Muat 20 grup berikutnya");
  const box = el("div", { class: "glist" }, rowsEl, moreBtn);
  container.append(box);

  let groups = [];
  let byId = new Map();
  let loaded = 0; // pages loaded so far
  let pages = 1; // pages on the server
  let active = null;
  let loading = null;

  const url = (p) => `/api/review/groups/${encodeURIComponent(kind)}?page=${p}`;

  function rowOf(g) {
    const shown = g.members.slice(0, MAX_MINI);
    const more = g.members.length - shown.length;
    const on = g.id === active;
    return el("button", {
      type: "button", class: "glist-row" + (on ? " is-active" : ""), role: "option", tabindex: "-1",
      "aria-selected": String(on), "data-id": g.id, title: g.summary || null,
      onclick: (ev) => { if (ev.detail > 0) ev.currentTarget.blur(); select(g.id); },
    },
    el("span", { class: "glist-thumbs" },
      ...shown.map((m) => el("img", { src: m.thumb || "", alt: "", loading: "lazy", draggable: "false" })),
      more > 0 ? el("span", { class: "glist-more-n" }, `+${more}`) : null),
    el("span", { class: "glist-text" }, el("span", { class: "glist-date" }, fmtDate(g.taken_from)), statusEl(g)));
  }

  function paint() {
    rowsEl.replaceChildren(...(groups.length ? groups.map(rowOf)
      : [el("p", { class: "empty" }, "Tidak ada grup yang perlu direview di kategori ini.")]));
    moreBtn.hidden = loaded >= pages;
    moreBtn.disabled = Boolean(loading);
  }

  function setGroups(list) {
    groups = list;
    byId = new Map(groups.map((g) => [g.id, g]));
    paint();
    if (onLoadedPage) onLoadedPage(groups.map((g) => g.id));
  }

  // One load at a time; a second call waits for the first.
  function guard(fn) {
    if (loading) return loading;
    loading = (async () => { try { await fn(); } finally { loading = null; moreBtn.disabled = false; } })();
    moreBtn.disabled = true;
    return loading;
  }

  function loadMore() {
    return guard(async () => {
      if (loaded >= pages && loaded > 0) return;
      const d = await api("GET", url(loaded + 1));
      pages = d.pages;
      loaded = d.page;
      setGroups([...groups, ...d.groups.filter((g) => !byId.has(g.id))]);
    });
  }

  // Re-reads every loaded page (after Terapkan the list shrinks).
  function refresh() {
    return guard(async () => {
      const fresh = [];
      const want = Math.max(1, loaded);
      for (let p = 1; p <= want; p += 1) {
        const d = await api("GET", url(p));
        pages = d.pages;
        loaded = d.page;
        fresh.push(...d.groups.filter((g) => !fresh.some((x) => x.id === g.id)));
        if (d.page < p) break; // fewer pages than before
      }
      setGroups(fresh);
    });
  }

  function select(id) {
    if (!byId.has(id)) return;
    active = id;
    for (const row of rowsEl.children) {
      const on = row.dataset.id === id;
      row.classList.toggle("is-active", on);
      row.setAttribute("aria-selected", String(on));
      if (on && row.scrollIntoView) row.scrollIntoView({ block: "nearest" });
    }
    if (onSelect) onSelect(id);
  }

  // Repaints one row after its members' decisions changed.
  function repaint(id) {
    const g = byId.get(id);
    const old = rowsEl.querySelector(`[data-id="${CSS.escape(id)}"]`);
    if (g && old) old.replaceWith(rowOf(g));
  }

  const member = (gid, uid) => { const g = byId.get(gid); return g ? g.members.find((m) => m.unit_id === uid) : null; };

  // Stores a saved decision in the row's copy of the member and repaints the row.
  function setDecision(gid, uid, decision) {
    const m = member(gid, uid);
    if (m) { m.decision = decision; repaint(gid); }
  }

  // What POST /api/review/accept stored for `unitIds`: keeper kept, every other member discarded.
  function suggested(gids, unitIds) {
    const ids = new Set(unitIds);
    const want = new Map();
    for (const g of gids.map((id) => byId.get(id)).filter(Boolean)) {
      for (const m of g.members) {
        if (ids.has(m.unit_id)) want.set(m.unit_id, { gid: g.id, decision: m.unit_id === g.keeper_id ? "keep" : "discard" });
      }
    }
    return want;
  }

  return {
    el: box,
    setDecision,
    suggested,
    decisionOf: (gid, uid) => { const m = member(gid, uid); return m ? m.decision : undefined; },
    refresh,
    loadMore,
    select,
    repaint,
    loadedIds: () => groups.map((g) => g.id),
    group: (id) => byId.get(id) || null,
    get hasMore() { return loaded < pages; },
    get loading() { return Boolean(loading); },
  };
}
