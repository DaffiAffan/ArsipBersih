import { api, el } from "/static/core/api.js";
import { watchJob } from "/static/components/jobbar.js";
import { datesNav } from "/static/components/sidebar.js";
import { fmtNum } from "/static/core/format.js";
import { modalDialog } from "/static/components/dialog.js";
import { toast } from "/static/components/toast.js";

const $ = (id) => document.getElementById(id);
const MOVE_NOTE = "Foto pindah ke folder yang benar saat Rapikan berikutnya.";
const MONTHS = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli", "Agustus",
  "September", "Oktober", "November", "Desember"];

function showError(msg) {
  const box = $("dt-error");
  box.hidden = !msg;
  box.textContent = msg || "";
}

function fmtDay(iso) {
  const d = iso.slice(0, 10).split("-").map(Number);
  return `${d[2]} ${MONTHS[d[1] - 1].slice(0, 3)} ${d[0]}`;
}

// --- Tanggal acara ----------------------------------------------------------------------

function eventCard(ev) {
  const node = $("dt-event-tpl").content.firstElementChild.cloneNode(true);
  const q = (sel) => node.querySelector(sel);
  q(".dt-event-name").textContent = ev.event_name;
  q(".dt-changed").hidden = ev.status !== "changed";
  q(".dt-event-summary").textContent = ev.summary || "";
  q(".dt-event-meta").textContent =
    `Foto kamera: ${fmtDay(ev.camera_start)} – ${fmtDay(ev.camera_end)} · ` +
    `${fmtNum(ev.count)} foto WA akan diberi tanggal di rentang ini. ${MOVE_NOTE}`;
  q(".dt-samples").replaceChildren(...ev.samples.map((fid) =>
    el("img", { src: `/media/thumb/${fid}`, alt: "", loading: "lazy", width: "88", height: "88" })));
  const start = q(".dt-start");
  const end = q(".dt-end");
  start.value = ev.camera_start.slice(0, 10);
  end.value = ev.camera_end.slice(0, 10);
  const range = q(".dt-range");
  const edit = q(".dt-edit");
  edit.addEventListener("click", () => {
    range.hidden = !range.hidden;
    edit.setAttribute("aria-expanded", String(!range.hidden));
    if (!range.hidden) start.focus();
  });
  const buttons = node.querySelectorAll("button");
  const busy = (on) => buttons.forEach((b) => { b.disabled = on; });
  const key = encodeURIComponent(ev.event_key);

  q(".dt-approve").addEventListener("click", async () => {
    if (!start.value || !end.value) { showError("Isi tanggal mulai dan tanggal selesai dulu."); return; }
    if (start.value > end.value) { showError("Tanggal mulai harus sebelum atau sama dengan tanggal selesai."); return; }
    showError("");
    busy(true);
    try {
      const r = await api("POST", `/api/dates/events/${key}/approve`,
        { range_start: start.value, range_end: end.value });
      toast(`${ev.event_name}: ${fmtNum(r.dated)} foto WA diberi tanggal acara. ${MOVE_NOTE}`, { kind: "ok" });
      await refresh();
    } catch (e) { showError(e.message); busy(false); }
  });
  q(".dt-ignore").addEventListener("click", async () => {
    showError("");
    busy(true);
    try {
      await api("POST", `/api/dates/events/${key}/ignore`);
      toast(`${ev.event_name}: foto WA tetap memakai tanggal kirim.`);
      await refresh();
    } catch (e) { showError(e.message); busy(false); }
  });
  return node;
}

async function loadEvents() {
  const { events } = await api("GET", "/api/dates/events");
  $("dt-events").replaceChildren(...events.map(eventCard));
  $("dt-events-empty").hidden = events.length > 0;
}

// --- Tanpa tanggal ----------------------------------------------------------------------

const PAGE = 200;
let order = [];            // every undated unit id, in grid order
const selected = new Set();
let anchor = null;         // index of the last plain/ctrl click, for Shift+click ranges
let shown = 0;

function cells() { return [...$("dt-undated").children]; }

function paintSelection() {
  for (const c of cells()) {
    const on = selected.has(c.dataset.unit);
    c.classList.toggle("is-selected", on);
    c.setAttribute("aria-selected", String(on));
  }
  const n = selected.size;
  $("dt-selected").textContent = n ? `${fmtNum(n)} dipilih` : "";
  $("dt-fill").disabled = n === 0;
  $("dt-leave").disabled = n === 0;
}

function onCellClick(ev, idx) {
  const id = order[idx];
  if (ev.shiftKey && anchor != null) {
    const [a, b] = anchor < idx ? [anchor, idx] : [idx, anchor];
    for (let i = a; i <= b; i++) selected.add(order[i]);
  } else {
    if (selected.has(id)) selected.delete(id); else selected.add(id);
    anchor = idx;
  }
  paintSelection();
}

function cell(item, idx) {
  const node = el("div", {
    class: "photo-cell dt-cell", role: "option", tabindex: "0", "data-unit": item.unit_id,
    "aria-selected": "false", title: item.name,
  },
  item.thumb ? el("img", { src: item.thumb, alt: item.name, loading: "lazy" }) : null,
  el("span", { class: "dt-check", "aria-hidden": "true" }),
  el("span", { class: "dt-name" }, item.name));
  node.addEventListener("click", (e) => onCellClick(e, idx));
  node.addEventListener("keydown", (e) => {
    if (e.key === " " || e.key === "Enter") { e.preventDefault(); onCellClick(e, idx); }
  });
  return node;
}

async function loadUndated({ more = false } = {}) {
  const offset = more ? shown : 0;
  const r = await api("GET", `/api/dates/undated?offset=${offset}&limit=${PAGE}`);
  order = r.unit_ids;
  const grid = $("dt-undated");
  const nodes = r.items.map((it, i) => cell(it, offset + i));
  if (more) grid.append(...nodes); else grid.replaceChildren(...nodes);
  shown = offset + r.items.length;
  const still = new Set(order);
  for (const id of [...selected]) if (!still.has(id)) selected.delete(id);
  if (!more) anchor = null;
  $("dt-undated-count").textContent = r.total ? `${fmtNum(r.total)} foto` : "";
  $("dt-undated-empty").hidden = r.total > 0;
  $("dt-more").hidden = shown >= r.total;
  $("dt-select-all").hidden = r.total === 0;
  paintSelection();
}

function numberField(label, { min, max, placeholder = "" }) {
  const input = el("input", { type: "number", class: "input", min: String(min), max: String(max), placeholder, inputmode: "numeric" });
  return { input, node: el("label", { class: "dt-field" }, el("span", {}, label), input) };
}

async function fillDates() {
  const ids = [...selected];
  if (!ids.length) return;
  const year = numberField("Tahun (wajib)", { min: 1900, max: new Date().getFullYear() + 1, placeholder: "2019" });
  const month = el("select", { class: "input", "aria-label": "Bulan" },
    el("option", { value: "" }, "Pilih bulan"),
    ...MONTHS.map((m, i) => el("option", { value: String(i + 1) }, m)));
  const day = numberField("Hari (boleh kosong)", { min: 1, max: 31 });
  const err = el("p", { class: "dt-dialog-error", role: "alert" });
  const values = await modalDialog({
    title: `Isi tanggal untuk ${fmtNum(ids.length)} foto`,
    okLabel: "Simpan tanggal",
    nodes: [
      year.node,
      el("label", { class: "dt-field" }, el("span", {}, "Bulan (wajib)"), month),
      day.node,
      el("p", { class: "muted" }, `Tanggal ini menggantikan tanggal lain untuk foto yang dipilih. ${MOVE_NOTE}`),
      err,
    ],
    value: () => ({ year: year.input.value, month: month.value, day: day.input.value }),
    cancelValue: null,
    focus: year.input,
    submitOnEnter: year.input,
  });
  if (!values) return;
  if (!/^\d{4}$/.test(values.year) || !values.month) {
    showError("Tahun dan bulan wajib diisi.");
    return;
  }
  const body = { unit_ids: ids, year: Number(values.year), month: Number(values.month) };
  if (values.day) body.day = Number(values.day);
  showError("");
  try {
    const r = await api("POST", "/api/dates/manual", body);
    selected.clear();
    toast(`${fmtNum(r.dated)} foto diberi tanggal. ${MOVE_NOTE}`, { kind: "ok" });
    await refresh();
  } catch (e) { showError(e.message); }
}

$("dt-fill").addEventListener("click", fillDates);
$("dt-leave").addEventListener("click", () => { selected.clear(); anchor = null; paintSelection(); });
$("dt-select-all").addEventListener("click", () => { order.slice(0, shown).forEach((id) => selected.add(id)); paintSelection(); });
$("dt-more").addEventListener("click", () => loadUndated({ more: true }).catch((e) => showError(e.message)));

// --- Tulis .xmp -------------------------------------------------------------------------

let writeJobId = null;
$("dt-write").addEventListener("click", async () => {
  showError("");
  try {
    const st = await api("POST", "/api/dates/write");
    writeJobId = st.id;
    $("dt-write").disabled = true;
  } catch (e) { showError(e.message); }
});

watchJob((st) => {
  if (!writeJobId || st.id !== writeJobId || st.status === "running") return;
  writeJobId = null;
  $("dt-write").disabled = false;
  if (st.status === "done") {
    const r = st.result || {};
    toast(r.text || "Tanggal selesai ditulis.", { kind: r.error_count ? "error" : "ok" });
    if (r.error_count) showError(`Sebagian gagal ditulis:\n${(r.errors || []).join("\n")}`);
  } else if (st.status === "failed") {
    showError(st.error || "Menulis tanggal gagal.");
  }
});

// --- load -------------------------------------------------------------------------------

async function refresh() {
  try {
    await Promise.all([loadEvents(), loadUndated()]);
  } catch (e) { showError(e.message); }
  datesNav.refresh();
}

refresh();
