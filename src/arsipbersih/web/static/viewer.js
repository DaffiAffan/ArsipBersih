import { el, fmtBytes } from "./api.js";
import { bindKeys } from "./keys.js";
import { createZoom } from "./zoom.js";

const CATEGORY_LABELS = {
  duplicate: "Duplikat", near_duplicate: "Mirip", burst: "Burst", blur: "Buram",
  screenshot: "Tangkapan layar", meme: "Meme", document: "Dokumen", junk: "Sampah",
};

function catLabel(c) { return CATEGORY_LABELS[c] || c; }

function preload(item) {
  if (item && item.media && item.media.kind !== "video" && item.media.view) new Image().src = item.media.view;
}

function fmtNum(v, digits) { return Number(v).toFixed(digits).replace(".", ","); }

function fmtDate(item) {
  if (!item.taken_at) return "Tidak diketahui";
  const d = new Date(item.taken_at);
  const s = isNaN(d) ? String(item.taken_at) : d.toLocaleString("id-ID");
  return item.date_source ? `${s} (${item.date_source})` : s;
}

function row(label, value) {
  return el("div", { class: "vw-row" }, el("dt", {}, label), el("dd", {}, value));
}

function infoPanel(item) {
  const p = item.primary || {};
  const dl = el("dl", { class: "vw-info" },
    row("Nama file", p.name || "-"),
    row("Resolusi", p.width && p.height ? `${p.width} × ${p.height}` : "-"),
    row("Ukuran", fmtBytes(p.size_total)),
    row("Sumber", p.source_label || "-"),
    row("Tanggal", fmtDate(item)),
    row("GPS", p.has_gps ? "ada" : "tidak ada"),
    row("Skor tajam", p.sharpness != null ? fmtNum(p.sharpness, 1) : "-"),
  );
  const box = el("div", { class: "vw-infobox" }, dl);
  const flags = item.flags || [];
  if (flags.length) {
    box.append(el("h4", {}, "Alasan"));
    for (const f of flags) {
      const score = f.score != null ? ` (${fmtNum(f.score, 2)})` : "";
      box.append(el("div", { class: "vw-flag" },
        el("span", { class: "vw-badge" }, catLabel(f.category)), ` ${f.reason || ""}${score}`));
    }
  }
  if (item.decision) {
    const label = item.decision === "keep" ? "simpan" : item.decision === "discard" ? "buang" : item.decision;
    box.append(el("div", { class: "vw-decision" }, `Keputusan: ${label}`));
  }
  return box;
}

function mediaPane(item) {
  const pane = el("div", { class: "vw-pane" });
  const m = item.media || {};
  if (m.kind === "video" && m.video) {
    const status = el("div", { class: "vw-status" }, "Menyiapkan video…");
    const video = el("video", { controls: true, preload: "auto", src: m.video });
    video.addEventListener("canplay", () => status.remove());
    video.addEventListener("error", () => {
      status.textContent = "Video tidak bisa diputar. Format atau file mungkin tidak didukung browser.";
      status.classList.add("vw-error");
    });
    pane.append(video, status);
    pane.isVideo = true;
  } else {
    pane.append(el("img", { src: m.view || m.thumb, alt: item.primary ? item.primary.name : "", draggable: "false" }));
  }
  return pane;
}

export function openViewer({ mode = "single", items, index = 0, onDecide, onIndex, onClose }) {
  let i = Math.min(Math.max(index, 0), items.length - 1);
  let zoom = null;
  const overlay = el("div", { class: "vw-overlay", role: "dialog", "aria-modal": "true" });
  const stage = el("div", { class: "vw-stage" });
  const side = el("aside", { class: "vw-side" });
  const counter = el("span", { class: "vw-counter" });
  const bar = el("div", { class: "vw-bar" }, counter,
    el("span", { class: "vw-hint" }, "← → pindah · K simpan · D buang · Enter terima saran · Spasi putar/jeda · Esc tutup"),
    el("button", { type: "button", onclick: () => close() }, "Tutup"));
  overlay.append(bar, el("div", { class: "vw-body" }, stage, side));
  document.body.append(overlay);

  // compare: items[i] = [kiri, kanan]; item aktif = panel kanan.
  function current() { return mode === "compare" ? items[i][1] : items[i]; }

  function render() {
    if (zoom) zoom.destroy();
    stage.replaceChildren();
    const shown = mode === "compare" ? items[i] : [items[i]];
    const panes = shown.map(mediaPane);
    panes.forEach((p) => stage.append(p));
    stage.classList.toggle("vw-compare", mode === "compare");
    const zoomable = panes.filter((p) => !p.isVideo);
    zoom = zoomable.length ? createZoom(zoomable) : null;
    side.replaceChildren();
    if (mode === "compare") {
      shown.forEach((it, n) => side.append(el("h3", {}, n === 0 ? "Kiri" : "Kanan (aktif)"), infoPanel(it)));
    } else {
      side.append(infoPanel(shown[0]));
    }
    counter.textContent = `${i + 1} / ${items.length}`;
    for (const j of [i + 1, i - 1]) {
      const x = items[j];
      if (x) (mode === "compare" ? x : [x]).forEach(preload);
    }
  }

  function go(n) {
    if (n < 0 || n >= items.length) return;
    i = n;
    render();
    if (onIndex) onIndex(i);
  }

  const unbind = bindKeys({
    ArrowLeft: () => go(i - 1),
    ArrowRight: () => go(i + 1),
    k: () => { if (onDecide) onDecide(current(), "keep"); },
    d: () => { if (onDecide) onDecide(current(), "discard"); },
    Enter: () => { if (onDecide) onDecide(current(), "suggest"); go(i + 1); },
    Escape: () => close(),
    " ": () => {
      const v = stage.querySelector("video");
      if (v) { if (v.paused) v.play(); else v.pause(); }
    },
  });

  let closed = false;
  function close() {
    if (closed) return;
    closed = true;
    unbind();
    if (zoom) zoom.destroy();
    overlay.remove();
    if (onClose) onClose();
  }

  render();
  return { close, go, get index() { return i; } };
}
