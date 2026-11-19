import { api, el, LOCATION_LABELS as LOCATIONS } from "/static/core/api.js";
import { fmtBytes, fmtNum } from "/static/core/format.js";
import { watchJob } from "/static/components/jobbar.js";
import { confirmDialog } from "/static/components/dialog.js";
import { toast } from "/static/components/toast.js";
import { CATEGORY_LABELS } from "/static/components/viewer.js";

function row(label, value) {
  return el("tr", {}, el("td", {}, label), el("td", { class: "num" }, typeof value === "number" ? fmtNum(value) : String(value)));
}

function table(title, rows) {
  return el("section", { class: "card" }, el("h2", {}, title), el("table", { class: "table report-table" }, el("tbody", {}, rows)));
}

async function load() {
  const r = await api("GET", "/api/report");
  const box = document.getElementById("report");
  box.className = "";
  box.replaceChildren(
    el("section", { class: "card report-total" }, el("span", { class: "report-big" }, fmtNum(r.total_media)), el("span", { class: "muted" }, " total file media")),
    table("Per lokasi", Object.entries(r.by_location).map(([k, v]) => row(LOCATIONS[k] || k, v))),
    table("Per kategori", Object.entries(r.by_category).map(([k, v]) => row(CATEGORY_LABELS[k] || k, v))),
    table("Lainnya", [
      row("Dihemat dari duplikat", fmtBytes(r.bytes_saved_duplicates)),
      row("Ukuran _sampah", fmtBytes(r.bytes_in_trash)),
      row("File non-media tertinggal", r.non_media_left),
      row("File tanpa tanggal", r.undated),
    ]),
  );
  document.getElementById("cache-size").textContent = fmtBytes(r.cache_bytes);
}

function showVerify(res) {
  const box = document.getElementById("verify-result");
  if (res.ok) {
    box.replaceChildren(el("p", { class: "report-ok" }, `Cocok: ${fmtNum(res.db_count)} file di DB = ${fmtNum(res.disk_count)} file di disk`));
    return;
  }
  const list = (title, items, total) => items.length
    ? el("div", {}, el("h3", {}, `${title} (${total})`),
        el("ul", {}, items.map((p) => el("li", {}, el("code", {}, p)))),
        total > items.length ? el("p", { class: "muted" }, `Menampilkan ${items.length} pertama dari ${total}.`) : null)
    : null;
  box.replaceChildren(...[
    el("p", {}, `Tidak cocok: ${fmtNum(res.db_count)} file di DB, ${fmtNum(res.disk_count)} file di disk`),
    list("File hilang", res.missing, res.missing_total),
    list("File tak terduga", res.unexpected, res.unexpected_total),
  ].filter(Boolean));
}

let lastSeen = null;
watchJob((st) => {
  const busy = st.status === "running";
  document.getElementById("verify-btn").disabled = busy;
  document.getElementById("cache-btn").disabled = busy;
  if (st.status === "running" || !st.id || st.id === lastSeen) return;
  if (st.kind !== "verify" && st.kind !== "cache") return;
  lastSeen = st.id;
  if (st.status === "failed") {
    const target = st.kind === "verify" ? "verify-result" : "cache-result";
    document.getElementById(target).replaceChildren(el("p", { class: "report-bad" }, `Gagal: ${st.error || ""}`));
  } else if (st.status === "done" && st.result) {
    if (st.kind === "verify") showVerify(st.result);
    else {
      const skipped = st.result.skipped || 0;
      const note = skipped ? ` · ${skipped} file sedang dipakai dan dilewati` : "";
      document.getElementById("cache-result").replaceChildren(el("p", {}, `Cache dihapus: ${fmtBytes(st.result.removed_bytes)}${note}`));
      load().catch(() => {});
    }
  }
});

document.getElementById("verify-btn").addEventListener("click", () => {
  document.getElementById("verify-result").replaceChildren(el("p", { class: "muted" }, "Memverifikasi..."));
  api("POST", "/api/report/verify").catch((e) => toast(e.message, { kind: "error" }));
});
document.getElementById("cache-btn").addEventListener("click", async () => {
  const ok = await confirmDialog({
    title: "Hapus cache?",
    body: "Semua cache thumbnail dan preview dihapus. File media tidak tersentuh; cache dibuat ulang otomatis.",
    okLabel: "Hapus cache",
  });
  if (!ok) return;
  api("POST", "/api/cache/clear").catch((e) => toast(e.message, { kind: "error" }));
});

load().catch((e) => { document.getElementById("report").textContent = e.message; });
