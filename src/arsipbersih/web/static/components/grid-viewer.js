// Opens the fullscreen viewer over a photo grid: placeholder cards for every loaded item, the
// full card (GET /api/review/unit/{id}) fetched for the current one and its neighbours.
import { api } from "/static/core/api.js";
import { openViewer } from "/static/components/viewer.js";

// onDecide(unitId, decision) -> Promise<boolean>; onShown(item): full media loaded (counts as
// seen, D-033); onChangeGroup(unitId, grp); onClose().
export function createGridViewer({ category, grid, onDecide, onShown, onChangeGroup, onClose, onError }) {
  let state = null; // { vitems, loaded:Set, handle }

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
    const vs = state;
    if (!vs || n < 0 || n >= vs.vitems.length) return;
    const it = grid.items[n];
    if (!it || vs.loaded.has(it.unit_id)) return;
    vs.loaded.add(it.unit_id);
    try {
      const card = await api("GET", `/api/review/unit/${encodeURIComponent(it.unit_id)}`);
      if (state !== vs || vs.vitems[n] === undefined) return;
      card.decision = it.decision; // grid state is the freshest
      vs.vitems[n] = card;
      if (vs.handle && vs.handle.index === n) vs.handle.go(n);
    } catch (e) {
      vs.loaded.delete(it.unit_id);
      onError(e);
    }
  }

  async function open(i) {
    const items = grid.items;
    if (state || i < 0 || i >= items.length) return;
    const vs = { vitems: items.map(placeholder), loaded: new Set(), handle: null };
    state = vs;
    grid.setFocus(i, false);
    await ensureCard(i);
    if (state !== vs) return;
    vs.handle = openViewer({
      mode: "single",
      items: vs.vitems,
      index: i,
      onDecide: async (card, decision) => {
        const ok = await onDecide(card.unit_id, decision === "suggest" ? null : decision);
        const it = grid.byId(card.unit_id);
        if (it && state === vs) {
          card.decision = it.decision;
          const vi = vs.vitems.findIndex((c) => c.unit_id === card.unit_id);
          if (vi >= 0) vs.vitems[vi].decision = it.decision;
          // No go(): that re-renders the stage (zoom reset, video restart). The viewer
          // repaints the info panel itself once onDecide resolves.
        }
        return ok;
      },
      onIndex: (n) => {
        grid.setFocus(n, false); // grid is behind the viewer: do not scroll it
        ensureCard(n);
        ensureCard(n + 1);
        ensureCard(n - 1);
        if (n >= grid.items.length - 6) grid.loadMore();
      },
      onShown: (card) => onShown(grid.byId(card.unit_id)),
      onChangeGroup: (card, grp) => onChangeGroup(card.unit_id, grp),
      onClose: () => {
        state = null;
        const it = grid.focused();
        const cell = it && grid.cell(it.unit_id);
        if (cell && cell.scrollIntoView) cell.scrollIntoView({ block: "nearest" });
        onClose();
      },
    });
    ensureCard(i + 1);
    ensureCard(i - 1);
    if (i >= items.length - 6) grid.loadMore();
  }

  return {
    open,
    get isOpen() { return Boolean(state); },
    // New grid items: the open viewer can step onto them too.
    append(fresh) { if (state) for (const it of fresh) state.vitems.push(placeholder(it)); },
    // The grid item at index `i` was removed (it left the category).
    removed(i) {
      const vs = state;
      if (!vs || i < 0) return;
      vs.vitems.splice(i, 1);
      if (!grid.items.length) { if (vs.handle) vs.handle.close(); return; }
      if (vs.handle) vs.handle.go(Math.min(vs.handle.index > i ? vs.handle.index - 1 : vs.handle.index, grid.items.length - 1));
    },
    // Reload the card so the viewer's info panel shows a change made on the server.
    reloadCard(unitId) {
      if (!state) return;
      state.loaded.delete(unitId);
      ensureCard(grid.indexOf(unitId));
    },
  };
}
