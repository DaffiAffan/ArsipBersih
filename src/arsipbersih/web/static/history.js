import { api, el, fmtBytes } from "/static/api.js";
import { watchJob } from "/static/job.js";

const KIND = { rapikan: "Rapikan", apply: "Apply", undo: "Undo", purge: "Kosongkan sampah" };
const $ = (id) => document.getElementById(id);
let data = null;
let lastJobKey = null;

// errors (optional): engine messages, listed under a collapsible "Detail" like plan.js does.
function say(text, errors) {
  const box = $("hist-msg");
  const parts = text ? [el("p", {}, text)] : [];
  if (errors && errors.length) {
    parts.push(el("details", {}, el("summary", {}, `Detail (${errors.length})`), el("pre", {}, errors.join("\n"))));
  }
  box.replaceChildren(...parts);
}

// Resolves true when the owner confirms. `extra` is an optional input placed in the body.
function confirmDialog({ title, body, okLabel, danger, validate, extra }) {
  return new Promise((resolve) => {
    const dlg = el("dialog", { class: "hist-dialog" });
    const ok = el("button", { type: "button", class: danger ? "danger" : "primary" }, okLabel);
    const cancel = el("button", { type: "button" }, "Batal");
    if (validate) ok.disabled = !validate();
    const done = (v) => { dlg.close(); dlg.remove(); resolve(v); };
    ok.addEventListener("click", () => done(true));
    cancel.addEventListener("click", () => done(false));
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); done(false); });
    const bodyNodes = [].concat(body).map((t) => el("p", {}, t));
    dlg.append(el("h3", {}, title), ...bodyNodes);
    if (extra) {
      dlg.append(extra);
      extra.addEventListener("input", () => { ok.disabled = !validate(); });
    }
    dlg.append(el("div", { class: "actions" }, cancel, ok));
    document.body.append(dlg);
    dlg.showModal();
    if (extra) extra.focus();
  });
}

async function post(url, body) {
  say("");
  try {
    await api("POST", url, body);
  } catch (e) {
    say(e.message);
    await reload();
  }
}

function fmtTime(s) { return (s || "").replace("T", " ").slice(0, 19); }

function renderBatches() {
  const rows = data.batches.map((b) => el("tr", {},
    el("td", {}, fmtTime(b.created_at)),
    el("td", {}, KIND[b.kind] || b.kind),
    el("td", {}, b.label || ""),
    el("td", {}, b.done),
    el("td", {}, b.undone),
    el("td", {}, b.failed),
    el("td", {}, b.undoable ? el("button", { type: "button", onclick: () => undoBatch(b) }, "Undo batch ini") : ""),
  ));
  $("batch-rows").replaceChildren(...rows);
  $("batch-empty").hidden = rows.length > 0;
  $("undo-all").disabled = !data.batches.some((b) => b.undoable);
}

async function undoBatch(b) {
  const ok = await confirmDialog({
    title: "Undo batch ini?",
    body: `File dari batch "${b.label || KIND[b.kind] || b.kind}" kembali ke posisi asal. Unit yang jalurnya sudah terisi dilewati.`,
    okLabel: "Undo",
  });
  if (ok) await post("/api/undo", { batch_id: b.id });
}

async function undoAll() {
  const ok = await confirmDialog({
    title: "Undo semua? Semua file kembali ke posisi asal di Sumber.",
    body: "Semua batch rapikan dan apply yang masih bisa dibatalkan di-undo, dari yang terbaru.",
    okLabel: "Undo semua",
  });
  if (ok) await post("/api/undo", { all: true });
}

function renderPending() {
  $("pending-section").hidden = data.pending.length === 0;
  $("pending-list").replaceChildren(...data.pending.map((p) => el("div", { class: "hist-row" },
    el("span", { class: "path" }, `${p.src_rel} → ${p.dst_rel}`),
    el("button", { type: "button", onclick: () => release(p, false) }, "File memang hilang"),
    el("button", { type: "button", onclick: () => release(p, true) }, "Sengaja saya hapus"),
  )));
}

async function release(p, onPurpose) {
  const ok = await confirmDialog({
    title: onPurpose ? "Sengaja saya hapus?" : "File memang hilang?",
    body: [
      `${p.src_rel} → ${p.dst_rel}`,
      "Baris ini memblokir semua perpindahan sampai dilepas. File ini tidak ditemukan di disk, "
        + (onPurpose ? "dan akan ditandai sengaja dihapus." : "dan akan ditandai hilang."),
    ],
    okLabel: "Lepaskan",
  });
  if (ok) await post("/api/journal/release", { row_id: p.row_id, deleted_on_purpose: onPurpose });
}

function renderMissing() {
  $("missing-section").hidden = data.missing.length === 0;
  $("missing-list").replaceChildren(...data.missing.map((m) => el("div", { class: "hist-row" },
    el("span", { class: "path" }, m.rel_path),
    el("button", { type: "button", onclick: () => forget(m) }, "Tandai sengaja dihapus"),
  )));
}

async function forget(m) {
  const ok = await confirmDialog({
    title: "Tandai sengaja dihapus?",
    body: `${m.rel_path} tidak akan dicari lagi.`,
    okLabel: "Tandai",
  });
  if (ok) await post("/api/files/forget", { file_id: m.file_id });
}

function renderTrash() {
  const t = data.trash;
  $("trash-info").textContent = t.files ? `${t.files} file (${fmtBytes(t.bytes)}) di _sampah.` : "_sampah kosong.";
  $("purge-btn").disabled = t.files === 0;
}

async function purge() {
  const { files, bytes } = data.trash; // the numbers shown in the dialog are the ones sent
  const first = await confirmDialog({
    title: "Kosongkan _sampah?",
    body: `${files} file, ${fmtBytes(bytes)} akan dihapus PERMANEN. Harddisk eksternal tidak punya Recycle Bin, jadi file tidak bisa dipulihkan.`,
    okLabel: "Lanjut",
    danger: true,
  });
  if (!first) return;
  const input = el("input", { type: "text", autocomplete: "off", "aria-label": "Ketik HAPUS" });
  const second = await confirmDialog({
    title: "Konfirmasi terakhir",
    body: `Ketik HAPUS untuk menghapus permanen ${files} file.`,
    okLabel: "Hapus permanen",
    danger: true,
    extra: input,
    validate: () => input.value === "HAPUS",
  });
  if (second) await post("/api/trash/purge", { expected_files: files, expected_bytes: bytes, confirm: input.value });
}

async function reload() {
  try {
    data = await api("GET", "/api/history");
  } catch (e) {
    say(e.message);
    return;
  }
  renderBatches();
  renderPending();
  renderMissing();
  renderTrash();
}

function showResult(st) {
  if (st.status === "failed") { say(`Gagal: ${st.error || ""}`); return; }
  const r = st.result || {};
  const errors = (r.errors || []).map(String);
  const errs = errors.length;
  if (st.kind === "undo") say(`Undo selesai: ${r.moved_units ?? 0} unit kembali, ${r.failed_units ?? 0} gagal${errs ? ` (${errs} catatan)` : ""}.`, errors);
  else if (st.kind === "purge") say(`Dihapus permanen: ${r.deleted ?? 0} file${errs ? `, ${errs} masalah` : ""}.`, errors);
  else say("Selesai.");
}

$("undo-all").addEventListener("click", undoAll);
$("purge-btn").addEventListener("click", purge);
reload();

let firstPoll = true;
watchJob((st) => {
  const initial = firstPoll;
  firstPoll = false; // idle and running polls count too, so a later finished job is shown
  if (st.status === "running" || st.status === "idle") return;
  const key = `${st.id}:${st.status}`;
  if (key === lastJobKey) return;
  lastJobKey = key;
  if (!initial) showResult(st); // a job already finished before page load has no context here
  reload();
});
