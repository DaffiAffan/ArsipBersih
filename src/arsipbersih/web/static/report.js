import { api, el, fmtBytes } from "/static/api.js";
import { watchJob } from "/static/job.js";

const LOCATIONS = { source: "Sumber", archive: "Arsip", review: "_review", trash: "_sampah" };
const CATEGORIES = {
  "duplikat-persis": "Duplikat persis", "duplikat-versi": "Duplikat versi", burem: "Burem",
  gelap: "Gelap", screenshot: "Screenshot", rusak: "Rusak", "salinan-wa": "Salinan WhatsApp",
  burst: "Burst", mirip: "Mirip",
};

function row(label, value) {
  return el("tr", {}, el("td", {}, label), el("td", {}, String(value)));
}

function table(title, rows) {
  return el("div", {}, el("h3", {}, title), el("table", {}, el("tbody", {}, rows)));
}

async function load() {
  const r = await api("GET", "/api/report");
  const box = document.getElementById("report");
  box.className = "";
  box.replaceChildren(
    el("p", {}, "Total file media: ", el("strong", {}, r.total_media)),
    table("Per lokasi", Object.entries(r.by_location).map(([k, v]) => row(LOCATIONS[k] || k, v))),
    table("Per kategori", Object.entries(r.by_category).map(([k, v]) => row(CATEGORIES[k] || k, v))),
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
    box.replaceChildren(el("p", {}, `Cocok: ${res.db_count} file di DB = ${res.disk_count} file di disk`));
    return;
  }
  const list = (title, items, total) => items.length
    ? el("div", {}, el("h3", {}, `${title} (${total})`),
        el("ul", {}, items.map((p) => el("li", {}, el("code", {}, p)))),
        total > items.length ? el("p", { class: "muted" }, `Menampilkan ${items.length} pertama dari ${total}.`) : null)
    : null;
  box.replaceChildren(
    el("p", {}, `Tidak cocok: ${res.db_count} file di DB, ${res.disk_count} file di disk`),
    list("File hilang", res.missing, res.missing_total),
    list("File tak terduga", res.unexpected, res.unexpected_total),
  );
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
    document.getElementById(target).replaceChildren(el("p", { class: "vw-error" }, `Gagal: ${st.error || ""}`));
  } else if (st.status === "done" && st.result) {
    if (st.kind === "verify") showVerify(st.result);
    else {
      document.getElementById("cache-result").replaceChildren(el("p", {}, `Cache dihapus: ${fmtBytes(st.result.removed_bytes)}`));
      load().catch(() => {});
    }
  }
});

document.getElementById("verify-btn").addEventListener("click", () => {
  document.getElementById("verify-result").replaceChildren(el("p", { class: "muted" }, "Memverifikasi..."));
  api("POST", "/api/report/verify").catch((e) => alert(e.message));
});
document.getElementById("cache-btn").addEventListener("click", () => {
  if (!confirm("Hapus semua cache thumbnail dan preview? File media tidak tersentuh; cache dibuat ulang otomatis.")) return;
  api("POST", "/api/cache/clear").catch((e) => alert(e.message));
});

load().catch((e) => { document.getElementById("report").textContent = e.message; });
