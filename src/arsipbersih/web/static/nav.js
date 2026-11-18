import { api, el } from "/static/api.js";
import { watchJob } from "/static/job.js";
import { fmtNum } from "/static/ui.js";

// One /api/review/categories request serves every caller (the sidebar and the group floating
// bar both refresh after each decision). Calls made before a request starts share it; a call
// made while one is in flight queues exactly one follow-up, so the answer always reflects it.
let queued = null;
let inflight = null;
function loadCategories() {
  if (queued) return queued;
  const prev = inflight;
  const p = (async () => {
    if (prev) { try { await prev; } catch (_) { /* the follow-up retries */ } } else await null;
    queued = null;
    const req = api("GET", "/api/review/categories");
    inflight = req;
    try { return await req; } finally { if (inflight === req) inflight = null; }
  })();
  queued = p;
  return p;
}

export function mountReviewNav(ul) {
  // Resolves to the category list (null when the request failed), for other widgets to reuse.
  async function refresh() {
    let cats;
    try {
      cats = await loadCategories();
    } catch (_) { return null; } // keep the previous list
    if (!ul) return cats;
    const current = decodeURIComponent(location.pathname.split("/")[2] || "");
    ul.replaceChildren(...cats.map((c) => {
      const active = location.pathname.startsWith("/review/") && c.category === current;
      const done = c.done ?? 0;
      const pct = c.count ? Math.min(100, Math.round((done / c.count) * 100)) : 0;
      return el("li", {},
        el("a", {
          class: `nav-item nav-cat${active ? " is-active" : ""}`,
          href: `/review/${c.category}`,
          "aria-current": active ? "page" : false,
          title: c.help || false,
        },
          el("span", { class: "nav-cat-row" },
            el("span", { class: "nav-cat-label" }, c.label || c.category),
            el("span", { class: "nav-cat-count" }, fmtNum(c.count))),
          el("span", { class: "progress", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": String(c.count), "aria-valuenow": String(done), "aria-label": `${done} dari ${c.count} sudah ditangani` },
            el("i", { style: `width:${pct}%` }))));
    }));
    return cats;
  }
  return { refresh };
}

// "Tanggal" entry: open event proposals (plus undated units, when there are any).
export function mountDatesNav(countEl) {
  async function refresh() {
    if (!countEl) return null;
    let c;
    try { c = await api("GET", "/api/dates/counts"); } catch (_) { return null; }
    countEl.textContent = c.undated ? `${fmtNum(c.proposals)} · ${fmtNum(c.undated)}` : fmtNum(c.proposals);
    countEl.title = `${fmtNum(c.proposals)} acara menunggu keputusan, ${fmtNum(c.undated)} foto tanpa tanggal`;
    return c;
  }
  return { refresh };
}

const ul = document.getElementById("nav-review");
export const reviewNav = mountReviewNav(ul);
export const datesNav = mountDatesNav(document.getElementById("nav-dates-count"));

if (ul) {
  let lastKey = null;
  let first = true;
  reviewNav.refresh();
  datesNav.refresh();
  watchJob((st) => {
    if (st.status === "running" || st.status === "idle") { first = false; return; }
    const key = `${st.id}:${st.status}`;
    if (key === lastKey) return;
    lastKey = key;
    if (!first) { reviewNav.refresh(); datesNav.refresh(); }
    first = false;
  });
}
