import { api, el } from "/static/core/api.js";
import { load, save } from "/static/core/storage.js";
import { toast } from "/static/components/toast.js";

// Scan stages in pipeline order (pipeline.STAGES) with their Indonesian names.
export const STAGES = [
  ["inventory", "Inventaris"], ["pairing", "Pasangan file"], ["hash", "Hash"],
  ["metadata", "Metadata"], ["visual", "Analisis visual"], ["video", "Analisis video"], ["ai", "Klasifikasi AI"],
  ["grouping", "Kelompok"],
  ["classify", "Klasifikasi"], ["events", "Acara"],
  ["dates", "Tanggal"], ["plan", "Rencana"],
];
const STAGE_LABELS = Object.fromEntries(STAGES);

export function stageLabel(stage) { return STAGE_LABELS[stage] || stage; }

export function watchJob(onChange) {
  let timer = null;
  async function tick() {
    let delay = 5000;
    try {
      const st = await api("GET", "/api/job");
      onChange(st);
      if (st.status === "running") delay = 1000;
    } catch (_) { /* server busy or restarting; retry */ }
    timer = setTimeout(tick, delay);
  }
  tick();
  return () => clearTimeout(timer);
}

function eta(s) {
  if (s == null) return "";
  if (s < 90) return "sebentar lagi";
  return `sekitar ${Math.round(s / 60)} menit lagi`;
}

function announceText(st) {
  if (st.status === "running") return `${st.label || "Proses"} berjalan`;
  if (st.status === "done") return `${st.label || "Proses"} selesai`;
  if (st.status === "stopped") return `${st.label || "Proses"} dijeda`;
  if (st.status === "failed") return `${st.label || "Proses"} gagal`;
  return "";
}

const DONE_LINGER_MS = 5000;
const DISMISSED_KEY = "arsipbersih.job.dismissed";

// The bar shows only while a job runs. A finished job lingers for a few seconds (and is also
// announced by a toast); a failed one stays until the owner closes it. A job that was already
// finished when the page opened is never shown again.
export function mountJobBar(bar) {
  // The bar itself updates every second, so only state changes go to the screen-reader region.
  const live = el("p", { class: "sr-only", role: "status", "aria-live": "polite" });
  bar.after(live);
  let lastState = "";
  let nodes = null;
  let lastId = null;
  let sawRunning = null; // id of the job seen running on this page
  let hideTimer = null;
  let lingerId = null; // id of the finished job currently lingering
  let lingerOver = false;
  const hide = () => { bar.hidden = true; };
  watchJob((st) => {
    const state = `${st.kind}|${st.status}`;
    if (state !== lastState) { lastState = state; live.textContent = announceText(st); }
    if (st.status !== "done") { clearTimeout(hideTimer); hideTimer = null; lingerId = null; lingerOver = false; }
    if (st.status === "running") sawRunning = st.id;
    if (st.status === "idle" || st.status === "stopped") { hide(); return; }
    if (st.status === "failed" && load("session", DISMISSED_KEY) === st.id) { hide(); return; }
    if (st.status === "done") {
      if (sawRunning !== st.id || lingerOver) { hide(); return; } // finished before this page opened, or shown long enough
      if (lingerId !== st.id) {
        lingerId = st.id;
        toast(`${st.label || "Proses"} selesai`, { kind: "ok" });
        hideTimer = setTimeout(() => { lingerOver = true; hide(); }, DONE_LINGER_MS);
      }
    }
    bar.hidden = false;
    const p = st.progress;
    // Update persistent nodes in place so the Jeda button keeps keyboard focus.
    if (!nodes) {
      nodes = {
        label: el("strong", {}, ""),
        text: document.createTextNode(""),
        pause: el("button", { type: "button", onclick: () => api("POST", "/api/job/stop").catch(() => {}) }, "Jeda"),
        close: el("button", { type: "button", "aria-label": "Tutup", onclick: () => { save("session", DISMISSED_KEY, lastId); hide(); } }, "Tutup"),
      };
      bar.replaceChildren(nodes.label, nodes.text, " ", nodes.pause, nodes.close);
    }
    lastId = st.id;
    nodes.label.textContent = st.label || "";
    let text = "";
    if (st.status === "running") {
      if (p) text = ` · ${stageLabel(p.stage)} · ${p.done}${p.total != null ? "/" + p.total : ""}${p.eta_s != null ? ` · ${eta(p.eta_s)}` : ""}`;
    } else if (st.status === "done") text = " · selesai";
    else if (st.status === "failed") text = ` · gagal: ${st.error || ""}`;
    if (nodes.text.data !== text) nodes.text.data = text;
    nodes.pause.hidden = !(st.status === "running" && st.kind === "scan");
    nodes.close.hidden = st.status !== "failed";
  });
}
