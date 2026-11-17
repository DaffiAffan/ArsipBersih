import { api } from "/static/api.js";

// Dwell-based "seen" for grid cells (D-032).
//
// A cell counts as seen only after its real thumbnail stayed >= 50% on screen for `dwellMs`
// while the page was still. A countdown starts `settleMs` after the last scroll/wheel event,
// and every scroll/wheel cancels all running countdowns. A cell leaving the viewport is
// cancelled. `isReady()` must be true when the countdown ends; if it is not (placeholder,
// viewer or dialog on top), the countdown starts over.

export function createSeenTracker({ root = null, dwellMs = 1000, settleMs = 150, onSeen }) {
  const watched = new Map(); // cell -> { unitId, isReady }
  const visible = new Set(); // watched cells currently >= 50% in view
  const timers = new Map(); // cell -> timeout id
  let settleTimer = null;
  let settled = true;

  function cancel(cell) {
    const t = timers.get(cell);
    if (t !== undefined) { clearTimeout(t); timers.delete(cell); }
  }

  function cancelAll() {
    for (const t of timers.values()) clearTimeout(t);
    timers.clear();
  }

  function start(cell) {
    cancel(cell);
    if (!settled || document.visibilityState === "hidden") return;
    timers.set(cell, setTimeout(() => expire(cell), dwellMs));
  }

  function startAll() {
    for (const cell of visible) start(cell);
  }

  function expire(cell) {
    timers.delete(cell);
    const w = watched.get(cell);
    if (!w || !visible.has(cell)) return;
    let ready = false;
    try { ready = !!w.isReady(); } catch (_) { ready = false; }
    if (!ready) { start(cell); return; }
    unobserve(cell);
    onSeen(w.unitId);
  }

  function onScroll() {
    settled = false;
    cancelAll();
    clearTimeout(settleTimer);
    settleTimer = setTimeout(() => { settleTimer = null; settled = true; startAll(); }, settleMs);
  }

  function onVisibility() {
    if (document.visibilityState === "hidden") cancelAll();
    else onScroll(); // coming back counts like a fresh stop: settle, then a full dwell
  }

  const io = typeof IntersectionObserver === "undefined" ? null : new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (!watched.has(e.target)) continue;
      if (e.isIntersecting && e.intersectionRatio >= 0.5) {
        if (!visible.has(e.target)) { visible.add(e.target); start(e.target); }
      } else {
        visible.delete(e.target);
        cancel(e.target);
      }
    }
  }, { root, threshold: [0, 0.5, 1] });

  // Capture phase: scroll does not bubble, and the grid scrolls inside .content, not window.
  document.addEventListener("scroll", onScroll, { capture: true, passive: true });
  document.addEventListener("wheel", onScroll, { capture: true, passive: true });
  document.addEventListener("touchmove", onScroll, { capture: true, passive: true });
  document.addEventListener("visibilitychange", onVisibility);

  function observe(cell, unitId, isReady) {
    if (!io || watched.has(cell)) return;
    watched.set(cell, { unitId, isReady: isReady || (() => true) });
    io.observe(cell);
  }

  function unobserve(cell) {
    cancel(cell);
    visible.delete(cell);
    watched.delete(cell);
    if (io) io.unobserve(cell);
  }

  // Restart every visible cell's countdown from zero, e.g. after the viewer closed.
  function flush() {
    cancelAll();
    startAll();
  }

  return { observe, unobserve, flush };
}

// Batches "seen" ids to POST /api/review/seen with retry. Failed chunks go back in the queue
// and are retried after `delayMs`; on pagehide whatever is left goes out with keepalive.
export function createSeenQueue({ chunk = 1000, delayMs = 1000, onSent }) {
  const queue = new Set();
  let timer = null;
  let chain = Promise.resolve();

  function schedule() {
    if (!timer) timer = setTimeout(() => { timer = null; flush(); }, delayMs);
  }

  function add(id) {
    if (queue.has(id)) return false;
    queue.add(id);
    schedule();
    return true;
  }

  async function send() {
    while (queue.size) {
      const ids = [...queue];
      queue.clear();
      for (let i = 0; i < ids.length; i += chunk) {
        const part = ids.slice(i, i + chunk);
        try {
          await api("POST", "/api/review/seen", { unit_ids: part });
        } catch (_) {
          for (const id of ids.slice(i)) queue.add(id); // retry later
          return false;
        }
        if (onSent) onSent(part);
      }
    }
    return true;
  }

  // Resolves true only when every queued id reached the server.
  function flush() {
    if (timer) { clearTimeout(timer); timer = null; }
    const p = chain.then(send);
    chain = p.catch(() => false);
    return p.then((ok) => { if (!ok) schedule(); return ok; });
  }

  function flushOnLeave() {
    const ids = [...queue];
    queue.clear();
    for (let i = 0; i < ids.length; i += chunk) {
      try {
        fetch("/api/review/seen", {
          method: "POST",
          keepalive: true,
          headers: { "X-ArsipBersih": "1", "Content-Type": "application/json" },
          body: JSON.stringify({ unit_ids: ids.slice(i, i + chunk) }),
        }).catch(() => {});
      } catch (_) { /* leaving anyway */ }
    }
  }

  window.addEventListener("pagehide", flushOnLeave);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") flush(); });

  return { add, has: (id) => queue.has(id), flush };
}
