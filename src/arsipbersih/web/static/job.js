import { api, el } from "/static/api.js";

// Scan stages in pipeline order (pipeline.STAGES) with their Indonesian names.
export const STAGES = [
  ["inventory", "Inventaris"], ["pairing", "Pasangan file"], ["hash", "Hash"],
  ["metadata", "Metadata"], ["visual", "Analisis visual"], ["grouping", "Kelompok"],
  ["classify", "Klasifikasi"], ["events", "Acara"], ["plan", "Rencana"],
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

export function mountJobBar(bar) {
  watchJob((st) => {
    if (st.status === "idle") { bar.hidden = true; return; }
    bar.hidden = false;
    const p = st.progress;
    const parts = [el("strong", {}, st.label || "")];
    if (st.status === "running") {
      if (p) parts.push(` · ${stageLabel(p.stage)} · ${p.done}${p.total != null ? "/" + p.total : ""}`, p.eta_s != null ? ` · ${eta(p.eta_s)}` : "");
      if (st.kind === "scan") {
        parts.push(" ", el("button", { type: "button", onclick: () => api("POST", "/api/job/stop").catch(() => {}) }, "Pause"));
      }
    } else if (st.status === "done") parts.push(" · selesai");
    else if (st.status === "stopped") parts.push(" · di-pause");
    else if (st.status === "failed") parts.push(` · gagal: ${st.error || ""}`);
    bar.replaceChildren(...parts.filter((x) => x !== ""));
  });
}
