import { api, el, fmtBytes } from "/static/api.js";
import { watchJob } from "/static/job.js";

const $ = (id) => document.getElementById(id);
const checked = new Set();
let data = null;
let tidyJobSeen = false;

function showError(msg) {
  const box = $("plan-error");
  box.hidden = !msg;
  box.textContent = msg || "";
}

function summaryLines(s) {
  const lines = [el("p", {}, `Ke Arsip: ${s.archive_units} unit, ${fmtBytes(s.archive_bytes)}`)];
  for (const [cat, [units, bytes]] of Object.entries(s.review)) {
    lines.push(el("p", {}, `Ke _review/${cat}: ${units} unit, ${fmtBytes(bytes)}`));
  }
  lines.push(el("p", {}, `Unit dilewati: ${s.skipped.length}`));
  if (s.skipped.length) {
    lines.push(el("ul", {}, s.skipped.map(([id, why]) => el("li", {}, `${id}: ${why}`))));
  }
  lines.push(el("p", { class: s.space_ok ? "" : "plan-error" },
    s.space_ok ? "Ruang disk: cukup" : `Ruang disk: tidak cukup (${s.space_error})`));
  return lines;
}

async function override(kind, name, value) {
  showError("");
  try {
    data = await api("POST", "/api/events/override", { kind, name, value });
    checked.clear();
    render();
  } catch (e) { showError(e.message); }
}

function openMerge(ev) {
  const sel = $("merge-target");
  sel.replaceChildren(...data.events.filter((o) => o.key !== ev.key).map((o) => el("option", { value: o.name }, o.name)));
  $("merge-from").textContent = `Acara "${ev.name}" akan digabung ke:`;
  $("merge-yes").onclick = () => { $("merge-dialog").close(); if (sel.value) override("merge", ev.name, sel.value); };
  $("merge-no").onclick = () => $("merge-dialog").close();
  $("merge-dialog").showModal();
}

function eventRow(ev) {
  const btn = (label, fn) => el("button", { type: "button", onclick: fn }, label);
  return el("tr", { class: checked.has(ev.key) ? "checked" : "" },
    el("td", {}, ev.name),
    el("td", {}, ev.month || "-"),
    el("td", {}, String(ev.file_count)),
    el("td", {}, (ev.sources || []).join(", ")),
    el("td", { class: "sample" }, ev.sample_path),
    el("td", {}, el("div", { class: "actions" },
      btn("Setuju", () => { checked.add(ev.key); render(); }),
      btn("Bukan acara", () => override("not_event", ev.name)),
      btn("Ganti nama…", () => {
        const v = window.prompt(`Nama baru untuk "${ev.name}":`, ev.name);
        if (v && v.trim()) override("rename", ev.name, v.trim());
      }),
      btn("Gabungkan ke…", () => openMerge(ev)),
    )));
}

function render() {
  const s = data.summary;
  $("events-body").replaceChildren(...data.events.map(eventRow));
  $("events-empty").hidden = data.events.length > 0;
  $("summary").replaceChildren(...summaryLines(s));
  const ok = s.scan_done && s.space_ok;
  $("tidy-btn").disabled = !ok;
  $("tidy-hint").textContent = !s.scan_done
    ? "Scan terakhir belum selesai. Jalankan scan lagi dulu."
    : !s.space_ok ? "Ruang disk belum cukup." : "";
}

async function load() {
  showError("");
  try { data = await api("GET", "/api/plan"); render(); } catch (e) { showError(e.message); }
}

$("tidy-btn").addEventListener("click", () => {
  $("confirm-body").replaceChildren(...summaryLines(data.summary));
  $("confirm-dialog").showModal();
});
$("confirm-no").addEventListener("click", () => $("confirm-dialog").close());
$("confirm-yes").addEventListener("click", async () => {
  $("confirm-dialog").close();
  showError("");
  try { await api("POST", "/api/tidy"); tidyJobSeen = true; $("tidy-btn").disabled = true; }
  catch (e) { showError(e.message); }
});

function showResult(st) {
  const r = st.result || {};
  const body = [
    el("p", {}, `Dipindah: ${r.moved_units ?? 0} unit · gagal: ${r.failed_units ?? 0} · dilewati: ${r.skipped_units ?? 0}`),
  ];
  if ((r.errors || []).length) {
    body.push(el("details", {}, el("summary", {}, "Detail"), el("pre", {}, r.errors.join("\n"))));
  }
  body.push(el("p", {}, el("a", { href: "/review" }, "Lanjut ke Review")));
  $("result-body").replaceChildren(...body);
  $("result").hidden = false;
}

watchJob((st) => {
  if (st.kind !== "tidy" || !tidyJobSeen) return;
  if (st.status === "done") { tidyJobSeen = false; showResult(st); load(); }
  else if (st.status === "failed") { tidyJobSeen = false; showError(st.error || "Merapikan gagal."); load(); }
});

load();
