import { api, el, LOCATION_LABELS as LOCATIONS } from "/static/api.js";
import { STAGES, watchJob } from "/static/job.js";

const $ = (id) => document.getElementById(id);
let incomplete = false;
let running = false;
let hasSource = true;

function setScanButton() {
  const b = $("scan-btn");
  b.textContent = incomplete ? "Lanjutkan scan" : "Mulai scan";
  b.disabled = running || !hasSource;
  $("pause-btn").hidden = !running;
}

async function load() {
  const d = await api("GET", "/api/home");
  incomplete = d.scan_incomplete;
  $("scan-info").textContent = d.last_scan_at ? `Scan terakhir selesai: ${d.last_scan_at}`
    : (incomplete ? "Scan terakhir belum selesai." : "Belum pernah di-scan.");
  hasSource = d.has_source;
  if (!hasSource) $("scan-info").textContent = d.source_missing_text;
  $("tools").replaceChildren(...Object.entries(d.tools).map(([k, v]) =>
    el("li", {}, v ? `${k}: ${v}` : [`${k}: belum terpasang. Pasang dengan `, el("code", {}, d.hints[k])])));
  $("by-location").replaceChildren(...Object.entries(d.by_location).map(([k, n]) =>
    el("span", { class: "chip" }, `${LOCATIONS[k] || k}: ${n}`)));
  $("categories").replaceChildren(...d.categories.map((c) =>
    el("li", {}, el("a", { href: `/review/${encodeURIComponent(c.category)}` }, c.category), ` (${c.count})`)));
  $("warnings").replaceChildren(...d.warnings.map((w) => el("li", {}, w)));
  setScanButton();
}

function showProgress(st) {
  const isScan = st.kind === "scan";
  const cur = isScan && st.progress ? st.progress.stage : null;
  const idx = STAGES.findIndex(([k]) => k === cur);
  $("stages").replaceChildren(...STAGES.map(([, label], i) => {
    let note = "";
    if (isScan && st.status === "running" && i === idx) {
      note = ` · ${st.progress.done}${st.progress.total ? "/" + st.progress.total : ""}`;
    } else if (isScan && st.status === "done") note = " · selesai";
    else if (isScan && st.status !== "idle" && idx > i) note = " · selesai";
    return el("li", { class: i === idx && st.status === "running" ? "active" : "" }, label + note);
  }));
}

let lastStatus = null;
watchJob((st) => {
  running = st.status === "running" && st.kind === "scan";
  showProgress(st);
  if (lastStatus === "running" && st.status !== "running") load();
  else setScanButton();
  lastStatus = st.status;
});

$("scan-btn").addEventListener("click", async () => {
  try { await api("POST", "/api/scan"); running = true; setScanButton(); }
  catch (e) { $("scan-info").textContent = e.message; }
});
$("pause-btn").addEventListener("click", () => api("POST", "/api/job/stop").catch(() => {}));
$("root-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    await api("POST", "/api/root", { path: $("root-input").value });
    location.reload();
  } catch (e) { $("root-msg").textContent = e.message; }
});
load();
