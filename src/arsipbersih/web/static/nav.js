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

// Per-tab cache of the sidebar counts and scroll position, so page loads feel seamless.
const CATS_KEY = "arsipbersih.nav.cats";
const DATES_KEY = "arsipbersih.nav.dates";
const SCROLL_KEY = "arsipbersih.nav.scroll";
function load(key) {
  try { return JSON.parse(sessionStorage.getItem(key) || "null"); } catch (_) { return null; }
}
function store(key, value) {
  try { sessionStorage.setItem(key, JSON.stringify(value)); } catch (_) { /* best effort */ }
}

export function mountReviewNav(ul) {
  // Resolves to the category list (null when the request failed), for other widgets to reuse.
  async function refresh() {
    let cats;
    try {
      cats = await loadCategories();
    } catch (_) { return null; } // keep the previous list
    store(CATS_KEY, cats);
    render(cats);
    return cats;
  }
  function render(cats) {
    if (!ul) return;
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
  }
  // Draw the last known list right away so the sidebar does not pop in after every page load.
  const cached = load(CATS_KEY);
  if (Array.isArray(cached)) render(cached);
  return { refresh };
}

// "Tanggal" entry: open event proposals (plus undated units, when there are any).
export function mountDatesNav(countEl) {
  async function refresh() {
    if (!countEl) return null;
    let c;
    try { c = await api("GET", "/api/dates/counts"); } catch (_) { return null; }
    store(DATES_KEY, c);
    render(c);
    return c;
  }
  function render(c) {
    if (!countEl || !c) return;
    countEl.textContent = c.undated ? `${fmtNum(c.proposals)} · ${fmtNum(c.undated)}` : fmtNum(c.proposals);
    countEl.title = `${fmtNum(c.proposals)} acara menunggu keputusan, ${fmtNum(c.undated)} foto tanpa tanggal`;
  }
  render(load(DATES_KEY));
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

const sidebar = document.getElementById("sidebar");
if (sidebar) {
  const top = load(SCROLL_KEY);
  if (typeof top === "number") sidebar.scrollTop = top;
  addEventListener("pagehide", () => store(SCROLL_KEY, sidebar.scrollTop));
}

const toggle = document.getElementById("sidebar-toggle");
if (toggle) {
  const root = document.documentElement;
  const sync = () => {
    const collapsed = root.classList.contains("sidebar-collapsed");
    toggle.setAttribute("aria-expanded", String(!collapsed));
    toggle.setAttribute("aria-label", collapsed ? "Tampilkan menu" : "Sembunyikan menu");
  };
  sync();
  toggle.addEventListener("click", () => {
    const collapsed = root.classList.toggle("sidebar-collapsed");
    try { localStorage.setItem("arsipbersih.sidebar", collapsed ? "collapsed" : "open"); } catch (_) { /* best effort */ }
    sync();
  });
}
