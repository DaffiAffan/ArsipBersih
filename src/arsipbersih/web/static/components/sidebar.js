import { api, el } from "/static/core/api.js";
import { load, save } from "/static/core/storage.js";
import { watchJob } from "/static/components/jobbar.js";
import { fmtNum } from "/static/core/format.js";

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
// Review categories as sidebar groups ("Duplikat", "Dokumen", ...), in the server's order.
// Each item shows what is still undecided (count - done), or a check mark once nothing is left.
export function mountReviewNav(box) {
  // Resolves to the category list (null when the request failed), for other widgets to reuse.
  async function refresh() {
    let cats;
    try {
      cats = await loadCategories();
    } catch (_) { return null; } // keep the previous list
    save("session", CATS_KEY, cats);
    render(cats);
    return cats;
  }
  function item(c, current) {
    const active = location.pathname.startsWith("/review/") && c.category === current;
    const left = Math.max(0, (c.count ?? 0) - (c.done ?? 0));
    return el("li", {},
      el("a", {
        class: `nav-item${active ? " is-active" : ""}`,
        href: `/review/${c.category}`,
        "aria-current": active ? "page" : false,
        title: c.label || false,
      },
        el("span", { class: "nav-label" }, c.short_label || c.label || c.category),
        el("span", { class: `nav-count${left ? "" : " is-done"}`, "aria-hidden": "true" }, left ? fmtNum(left) : "✓"),
        el("span", { class: "sr-only" }, left ? `, ${fmtNum(left)} belum diputuskan` : ", selesai")));
  }
  function render(cats) {
    if (!box) return;
    const current = decodeURIComponent(location.pathname.split("/")[2] || "");
    const groups = [];
    for (const c of cats) {
      const key = c.group || "lain";
      let g = groups.find((x) => x.key === key);
      if (!g) groups.push(g = { key, title: c.group_title || "Review", items: [] });
      g.items.push(c);
    }
    box.replaceChildren(...groups.map((g) => {
      const id = `nav-group-${g.key}`;
      return el("div", { class: "nav-group" },
        el("div", { class: "nav-group-title", id }, g.title),
        el("ul", { class: "nav-list", "aria-labelledby": id }, ...g.items.map((c) => item(c, current))));
    }));
  }
  // Draw the last known list right away so the sidebar does not pop in after every page load.
  const cached = load("session", CATS_KEY);
  if (Array.isArray(cached)) render(cached);
  return { refresh };
}

// "Tanggal" entry: open event proposals (plus undated units, when there are any).
export function mountDatesNav(countEl) {
  async function refresh() {
    if (!countEl) return null;
    let c;
    try { c = await api("GET", "/api/dates/counts"); } catch (_) { return null; }
    save("session", DATES_KEY, c);
    render(c);
    return c;
  }
  function render(c) {
    if (!countEl || !c) return;
    countEl.textContent = c.undated ? `${fmtNum(c.proposals)} · ${fmtNum(c.undated)}` : fmtNum(c.proposals);
    countEl.title = `${fmtNum(c.proposals)} acara menunggu keputusan, ${fmtNum(c.undated)} foto tanpa tanggal`;
  }
  render(load("session", DATES_KEY));
  return { refresh };
}

const navBox = document.getElementById("nav-review");
export const reviewNav = mountReviewNav(navBox);
export const datesNav = mountDatesNav(document.getElementById("nav-dates-count"));

if (navBox) {
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
  const top = load("session", SCROLL_KEY);
  if (typeof top === "number") sidebar.scrollTop = top;
  addEventListener("pagehide", () => save("session", SCROLL_KEY, sidebar.scrollTop));
}

// The menu button. Wide screens: show/hide in the page flow and remember the choice. Below
// 1000 px the sidebar is an overlay over the content: closed by default, never remembered, so it
// is closed again after every page load.
const toggle = document.getElementById("sidebar-toggle");
if (toggle) {
  const root = document.documentElement;
  const narrow = matchMedia("(max-width: 999.98px)");
  const scrim = document.getElementById("sidebar-scrim");
  const isOpen = () => (narrow.matches ? root.classList.contains("sidebar-open") : !root.classList.contains("sidebar-collapsed"));
  const sync = () => {
    const open = isOpen();
    toggle.setAttribute("aria-expanded", String(open));
    toggle.setAttribute("aria-label", open ? "Sembunyikan menu" : "Tampilkan menu");
  };
  const closeOverlay = (refocus) => {
    if (!root.classList.contains("sidebar-open")) return;
    root.classList.remove("sidebar-open");
    sync();
    if (refocus) toggle.focus();
  };
  sync();
  toggle.addEventListener("click", () => {
    if (narrow.matches) {
      root.classList.toggle("sidebar-open");
    } else {
      const collapsed = root.classList.toggle("sidebar-collapsed");
      save("local", "arsipbersih.sidebar", collapsed ? "collapsed" : "open");
    }
    sync();
  });
  if (scrim) scrim.addEventListener("click", () => closeOverlay(true));
  addEventListener("keydown", (e) => { if (e.key === "Escape" && narrow.matches) closeOverlay(true); });
  if (sidebar) sidebar.addEventListener("click", (e) => { if (e.target.closest("a")) closeOverlay(false); });
  narrow.addEventListener("change", () => { root.classList.remove("sidebar-open"); sync(); });
}
