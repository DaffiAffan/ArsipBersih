import { api, el } from "/static/api.js";
import { stageLabel, watchJob } from "/static/job.js";
import { fmtBytes, fmtNum, fmtTime } from "/static/ui.js";

const $ = (id) => document.getElementById(id);
const TOOL_NAMES = { exiftool: "ExifTool", ffmpeg: "ffmpeg", ffprobe: "ffprobe" };
let data = null;
let job = { status: "idle" };

function reviewTarget(d) {
  const cats = d.categories || [];
  const open = cats.find((c) => c.count > c.done) || cats[0];
  return open ? `/review/${encodeURIComponent(open.category)}` : "/review";
}

function steps(d) {
  const t = d.steps.totals;
  const scanned = !!d.last_scan_at || d.scan_incomplete || t.units > 0;
  const left = Math.max(t.review - t.review_done, 0);
  return [
    {
      title: "Scan",
      text: t.units > 0 ? `${fmtNum(t.units)} item sudah dipindai.` : "Belum ada yang dipindai.",
      action: { label: d.scan_incomplete ? "Lanjutkan scan" : (scanned ? "Scan ulang" : "Mulai scan"), scan: true },
    },
    {
      title: "Cek acara & Rapikan",
      text: t.source > 0
        ? `${fmtNum(t.source)} item di Sumber menunggu dirapikan${t.events > 0 ? `, terbagi dalam ${fmtNum(t.events)} acara` : ""}.`
        : `${fmtNum(t.archive)} item sudah rapi di Arsip.`,
      action: { label: "Buka rencana", href: "/rencana" },
    },
    {
      title: "Review",
      text: t.review > 0
        ? `${fmtNum(left)} dari ${fmtNum(t.review)} masih menunggu review.`
        : "Tidak ada yang perlu di-review.",
      action: { label: "Mulai review", href: reviewTarget(d) },
    },
    {
      title: "Kosongkan sampah",
      text: t.trash_files > 0
        ? `${fmtNum(t.trash_files)} file (${fmtBytes(t.trash_bytes)}) menunggu di _sampah/.`
        : "_sampah/ kosong.",
      action: { label: "Buka riwayat", href: "/riwayat" },
    },
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
    el("button", { id: "pause-btn", type: "button", class: "btn btn-secondary", onclick: () => api("POST", "/api/job/stop").catch(() => {}) }, "Pause"));
}

// Patches the running Scan card in place so keyboard focus (Pause) survives every tick.
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

function renderSteps() {
  const d = data;
  if (!d) return;
  const scanRunning = job.status === "running" && job.kind === "scan";
  $("steps").replaceChildren(...steps(d).map((s, i) => {
    const n = i + 1;
    const cur = d.steps.current;
    const state = cur === n ? "now" : (cur === 0 || n < cur ? "done" : "later");
    const stateText = { now: "Sekarang", done: "Selesai", later: "Nanti" }[state];
    const live = n === 1 && scanRunning;
    let action;
    if (live) action = scanLive();
    else if (s.action.scan) {
      action = el("button", {
        type: "button", id: "scan-btn", disabled: !d.has_source,
        class: state === "now" ? "btn btn-primary" : "btn btn-secondary", onclick: startScan,
      }, s.action.label);
    } else {
      action = el("a", { href: s.action.href, class: state === "now" ? "btn btn-primary" : "btn btn-secondary" }, s.action.label);
    }
    return el("li", { class: `step step-${state}`, "aria-current": state === "now" ? "step" : undefined },
      el("div", { class: "step-head" },
        el("span", { class: "step-num", "aria-hidden": "true" }, state === "done" ? "✓" : String(n)),
        el("h2", {}, s.title),
        el("span", { class: state === "now" ? "badge" : "badge badge-muted" }, stateText)),
      el("p", { class: "step-text" }, live ? "Scan sedang berjalan." : s.text),
      action);
  }));
}

function render() {
  const d = data;
  $("last-scan").textContent = d.last_scan_at ? `Scan terakhir: ${fmtTime(d.last_scan_at)}`
    : (d.scan_incomplete ? "Scan terakhir belum selesai" : "Belum pernah di-scan");
  const w = $("source-warning");
  w.hidden = d.has_source;
  w.textContent = d.has_source ? "" : d.source_missing_text;
  $("warnings").replaceChildren(...d.warnings.map((x) => el("li", {}, x)));
  const t = d.steps.totals;
  $("summary").replaceChildren(...[
    ["Sumber", t.source], ["Arsip", t.archive], ["Menunggu review", Math.max(t.review - t.review_done, 0)], ["Sampah", t.trash_files],
  ].map(([k, v]) => el("div", { class: "stat" }, el("dd", {}, fmtNum(v)), el("dt", {}, k))));
  const missing = Object.keys(d.tools).filter((k) => !d.tools[k]);
  $("tools-line").replaceChildren(...(missing.length
    ? [el("span", { class: "tools-bad" }, `Alat belum terpasang: ${missing.map((k) => TOOL_NAMES[k] || k).join(", ")}. `),
       ...missing.map((k) => el("code", { class: "hint" }, d.hints[k] || ""))]
    : ["Alat: ExifTool, ffmpeg, ffprobe siap"]));
  $("tools").replaceChildren(...Object.entries(d.tools).map(([k, v]) =>
    el("li", {}, `${TOOL_NAMES[k] || k}: ${v || "belum terpasang"}`)));
  renderSteps();
}

async function load() {
  data = await api("GET", "/api/home");
  render();
}

async function startScan() {
  try {
    await api("POST", "/api/scan");
    job = { status: "running", kind: "scan" };
    last = "running";
    renderSteps();
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
      announce(st.status === "done" ? "Scan selesai" : (st.status === "stopped" ? "Scan di-pause" : "Scan gagal"));
    }
    load();
  } else if (data && is && was && patchLive()) { /* in-place update */ }
  else if (data && (was || is)) renderSteps();
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
