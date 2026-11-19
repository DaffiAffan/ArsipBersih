import { api } from "/static/core/api.js";

// "Seen" ids come only from the fullscreen viewer (D-033); the grid dwell tracker is gone.

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
