import { api, el } from "/static/core/api.js";
import { stageLabel, watchJob } from "/static/components/jobbar.js";
import { renderSteps } from "/static/components/steps.js";
import { fmtBytes, fmtNum, fmtTime } from "/static/core/format.js";

const $ = (id) => document.getElementById(id);
const TOOL_NAMES = { exiftool: "ExifTool", ffmpeg: "ffmpeg", ffprobe: "ffprobe" };
let data = null;
let dateProposals = 0;
let job = { status: "idle" };

const left = (c) => Math.max(c.count - c.done, 0);
function topCats(d) {
  return (d.categories || []).filter((c) => left(c) > 0).sort((a, b) => left(b) - left(a));
}
function reviewHref(c) { return c ? `/review/${encodeURIComponent(c.category)}` : "/review"; }

function title(d) {
  const t = d.steps.totals;
  switch (d.steps.current) {
    case 1: return "Mulai dengan scan";
    case 2: return "Siap merapikan";
    case 3: return `${fmtNum(t.review_units)} foto & video menunggu review`;
    case 4: return "Kosongkan sampah bila sudah yakin";
    default: return "Semua sudah direview";
  }
}

function link(label, href) {
  return el("a", { href, class: "btn btn-primary" }, label);
}

function stepList(d) {
  const t = d.steps.totals;
  const cur = d.steps.current;
  const scanned = !!d.last_scan_at || d.scan_incomplete || t.units > 0;
  const state = (n) => (cur === n ? "current" : (cur === 0 || n < cur ? "done" : "later"));
  const act = (n, node) => (cur === n ? node : null);
  const scanRunning = job.status === "running" && job.kind === "scan";
  const scanLabel = d.scan_incomplete ? "Lanjutkan scan" : (scanned ? "Scan ulang" : "Mulai scan");
  const scanBtn = scanRunning ? scanLive() : el("button", {
    type: "button", id: "scan-btn", disabled: !d.has_source, class: cur === 1 ? "btn btn-primary" : "btn btn-secondary", onclick: startScan,
  }, scanLabel);
  const top = topCats(d)[0];
  const reviewPct = t.review > 0 ? Math.round((t.review_done / t.review) * 100) : 100;
  return [
    { title: "Scan", state: state(1), action: scanBtn,
      text: scanRunning ? "Scan sedang berjalan." : (t.units > 0 ? `${fmtNum(t.units)} item sudah dipindai.` : "Belum ada yang dipindai.") },
    { title: "Cek acara & Rapikan", state: state(2), action: act(2, link("Buka rencana", "/rencana")),
      text: t.source > 0
        ? `${fmtNum(t.source)} item di Sumber menunggu dirapikan${t.events > 0 ? `, terbagi dalam ${fmtNum(t.events)} acara` : ""}.`
        : `${fmtNum(t.archive)} item sudah rapi di Arsip.` },
    { title: "Review", state: state(3), meter: cur === 3 ? reviewPct : undefined,
      action: act(3, link(top ? `Lanjutkan review: ${top.label}` : "Mulai review", reviewHref(top))),
      text: t.review > 0 ? `${fmtNum(Math.max(t.review - t.review_done, 0))} dari ${fmtNum(t.review)} masih menunggu review.` : "Tidak ada yang perlu di-review." },
    { title: "Kosongkan sampah", state: state(4), action: act(4, link("Buka riwayat", "/riwayat")),
      text: t.trash_files > 0 ? `${fmtNum(t.trash_files)} file (${fmtBytes(t.trash_bytes)}) menunggu di Sampah.` : "Sampah kosong." },
  ];
}

function liveText() {
  const p = job.progress;
  return p ? `${stageLabel(p.stage)} · ${fmtNum(p.done)}${p.total ? " / " + fmtNum(p.total) : ""}` : "Memulai…";
}

function livePct() {
  const p = job.progress;
  return p && p.total ? Math.min(100, Math.round((p.done / p.total) * 100)) : null;
}

function scanLive() {
  const pct = livePct();
  return el("div", { class: "step-live" },
    el("p", { id: "scan-stage" }, liveText()),
    el("span", { id: "scan-bar", class: "progress", role: "progressbar", "aria-label": "Progres scan", "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": pct ?? undefined },
      el("i", { style: `width:${pct ?? 100}%${pct == null ? ";opacity:.4" : ""}` })),
    el("button", { id: "pause-btn", type: "button", class: "btn btn-secondary", onclick: () => api("POST", "/api/job/stop").catch(() => {}) }, "Jeda"));
}

// Patches the running Scan card in place so keyboard focus (Jeda) survives every tick.
function patchLive() {
  const stage = $("scan-stage");
  if (!stage) return false;
  stage.textContent = liveText();
  const bar = $("scan-bar");
  const pct = livePct();
  if (pct == null) bar.removeAttribute("aria-valuenow"); else bar.setAttribute("aria-valuenow", String(pct));
  const i = bar.firstElementChild;
  i.style.width = `${pct ?? 100}%`;
  i.style.opacity = pct == null ? ".4" : "";
  return true;
}

let announcedStage = null;
function announce(text) { $("scan-announce").textContent = text; }

function renderTimeline() {
  if (!data) return;
  renderSteps($("steps"), stepList(data));
}

function renderSide(d) {
  const top = topCats(d).slice(0, 5);
  $("top-cats").replaceChildren(...(top.length
    ? top.map((c) => el("li", {}, el("a", { href: reviewHref(c) }, c.label), el("span", { class: "list-meta" }, fmtNum(left(c)))))
    : [el("li", { class: "muted" }, "Semua kategori sudah diputuskan.")]));
  $("needs").replaceChildren(...(dateProposals > 0
    ? [el("li", {}, el("a", { href: "/tanggal" }, "Usulan tanggal acara"), el("span", { class: "list-meta" }, fmtNum(dateProposals)))]
    : [el("li", { class: "muted" }, "Tidak ada yang perlu kamu putuskan.")]));
}

function render() {
  const d = data;
  $("home-title").textContent = title(d);
  const t = d.steps.totals;
  const lastScan = d.last_scan_at ? `Scan terakhir: ${fmtTime(d.last_scan_at)}`
    : (d.scan_incomplete ? "Scan terakhir belum selesai" : "Belum pernah di-scan");
  const missing = Object.keys(d.tools).filter((k) => !d.tools[k]);
  const toolText = missing.length ? `Alat belum terpasang: ${missing.map((k) => TOOL_NAMES[k] || k).join(", ")}` : "Alat siap";
  $("home-line").textContent = `${lastScan} · ${fmtNum(t.units)} file · ${toolText}`;
  const w = $("source-warning");
  w.hidden = d.has_source;
  w.textContent = d.has_source ? "" : d.source_missing_text;
  $("warnings").replaceChildren(...[...d.warnings, ...(d.clock_warnings || [])].map((x) => el("li", {}, x)));
  $("tools-line").replaceChildren(...(missing.length
    ? missing.map((k) => el("code", { class: "hint" }, d.hints[k] || ""))
    : []),
    ...(d.ai_model.ready
      ? ["Model AI: siap"]
      : [el("span", { class: "tools-bad" }, "Model AI: belum disiapkan. Jalankan "), el("code", { class: "hint" }, "arsipbersih setup-models")]));
  $("tools").replaceChildren(...Object.entries(d.tools).map(([k, v]) =>
    el("li", {}, `${TOOL_NAMES[k] || k}: ${v || "belum terpasang"}`)));
  renderSide(d);
  renderTimeline();
}

async function load() {
  data = await api("GET", "/api/home");
  try { dateProposals = (await api("GET", "/api/dates/counts")).proposals || 0; } catch { dateProposals = 0; }
  render();
}

async function startScan() {
  try {
    await api("POST", "/api/scan");
    job = { status: "running", kind: "scan" };
    last = "running";
    renderTimeline();
  } catch (e) {
    const w = $("source-warning");
    w.hidden = false;
    w.textContent = e.message;
  }
}

let last = null;
watchJob((st) => {
  const prev = job;
  job = st;
  const was = prev.status === "running" && prev.kind === "scan";
  const is = st.status === "running" && st.kind === "scan";
  if (is) {
    const stage = st.progress ? st.progress.stage : "";
    if (stage !== announcedStage) { announcedStage = stage; announce(stage ? `Scan berjalan: ${stageLabel(stage)}` : "Scan dimulai"); }
  }
  if (last === "running" && st.status !== "running") {
    if (st.kind === "scan") {
      announcedStage = null;
      announce(st.status === "done" ? "Scan selesai" : (st.status === "stopped" ? "Scan dijeda" : "Scan gagal"));
    }
    load();
  } else if (data && is && was && patchLive()) { /* in-place update */ }
  else if (data && (was || is)) renderTimeline();
  last = st.status;
});

$("root-toggle").addEventListener("click", () => {
  const f = $("root-form");
  f.hidden = !f.hidden;
  $("root-toggle").setAttribute("aria-expanded", String(!f.hidden));
  if (!f.hidden) $("root-input").focus();
});
$("root-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  try {
    await api("POST", "/api/root", { path: $("root-input").value });
    location.reload();
  } catch (e) { $("root-msg").textContent = e.message; }
});
load();
