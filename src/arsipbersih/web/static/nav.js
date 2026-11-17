import { api, el } from "/static/api.js";
import { watchJob } from "/static/job.js";
import { fmtNum } from "/static/ui.js";

export function mountReviewNav(ul) {
  async function refresh() {
    let cats;
    try {
      cats = await api("GET", "/api/review/categories");
    } catch (_) { return; } // keep the previous list
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
  return { refresh };
}

const ul = document.getElementById("nav-review");
export const reviewNav = ul ? mountReviewNav(ul) : { refresh: async () => {} };

if (ul) {
  let lastKey = null;
  let first = true;
  reviewNav.refresh();
  watchJob((st) => {
    if (st.status === "running" || st.status === "idle") { first = false; return; }
    const key = `${st.id}:${st.status}`;
    if (key === lastKey) return;
    lastKey = key;
    if (!first) reviewNav.refresh();
    first = false;
  });
}
