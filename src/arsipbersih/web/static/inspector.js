// Info panel for one unit (shape of GET /api/review/unit/{id}). Used by the grid, the group
// screen and the fullscreen viewer.
import { api, el, fmtBytes } from "/static/api.js";
import { CATEGORY_LABELS } from "/static/viewer.js";

const DATE_SOURCES = { filename: "dari nama file", exif: "EXIF", quicktime: "QuickTime" };
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"];
export const DISCARD_EFFECT = "Kalau tidak di-Keep, file pindah ke _sampah saat Apply (bisa di-Undo)";

function fmtTaken(iso) {
  if (!iso) return "Tidak diketahui";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const date = `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
  if (d.getHours() === 0 && d.getMinutes() === 0 && d.getSeconds() === 0) return date;
  const hm = `${String(d.getHours()).padStart(2, "0")}.${String(d.getMinutes()).padStart(2, "0")}`;
  return `${date}, ${hm}`;
}

function fmtDuration(s) {
  const n = Math.round(Number(s) || 0);
  return `${Math.floor(n / 60)}:${String(n % 60).padStart(2, "0")}`;
}

function row(label, ...value) {
  return [el("dt", {}, label), el("dd", {}, ...value)];
}

function folderOf(path) {
  const i = path ? path.lastIndexOf("/") : -1;
  return i > 0 ? path.slice(0, i) : "";
}

export function renderInspector(node, unit, { onDecide } = {}) {
  if (!unit) {
    node.replaceChildren(el("p", { class: "empty" }, "Pilih foto untuk melihat infonya."));
    return;
  }
  const p = unit.primary || {};
  const file = (unit.files || [])[0] || {};
  const media = unit.media || {};
  const name = p.name || file.name || "-";
  const path = file.rel_path || "";

  const preview = media.thumb
    ? el("img", { class: "insp-preview", src: media.thumb, alt: name, draggable: "false" })
    : el("div", { class: "insp-preview insp-noprev" }, "Pratinjau tidak tersedia");

  const dateSrc = DATE_SOURCES[unit.date_source];
  const facts = el("dl", { class: "insp-facts" },
    ...row("Tanggal", fmtTaken(unit.taken_at), dateSrc ? el("span", { class: "muted" }, ` · ${dateSrc}`) : null),
    ...row("Ukuran", p.size_total != null ? fmtBytes(p.size_total) : "-",
      p.width && p.height ? ` · ${p.width} × ${p.height}` : ""),
    ...(p.duration_s != null ? row("Durasi", fmtDuration(p.duration_s)) : []),
    ...row("Sumber", p.source_label || "-"),
    ...row("Folder", el("span", { class: "mono insp-path" }, folderOf(path) || "-")),
  );

  const flags = unit.flags || [];
  const why = el("section", { class: "insp-why" },
    el("h4", {}, "Kenapa masuk sini"),
    flags.length
      ? el("ul", {}, ...flags.map((f) => el("li", {},
        el("strong", {}, CATEGORY_LABELS[f.category] || f.category),
        f.reason ? ` · ${f.reason}` : "")))
      : el("p", { class: "muted" }, "Tidak ada keterangan."),
    el("p", { class: "insp-effect" }, DISCARD_EFFECT));

  const d = unit.decision;
  const decide = (want) => { if (onDecide) onDecide(d === want ? null : want); };
  const keepBtn = el("button", { type: "button", class: "btn btn-keep", "aria-pressed": String(d === "keep"), onclick: () => decide("keep") },
    "Keep ", el("kbd", { class: "kbd" }, "S"));
  const delBtn = el("button", { type: "button", class: "btn btn-danger", "aria-pressed": String(d === "discard"), onclick: () => decide("discard") },
    "Delete ", el("kbd", { class: "kbd" }, "D"));
  const status = d === "keep" ? el("span", { class: "badge badge-keep" }, "Keep")
    : d === "discard" ? el("span", { class: "badge badge-danger" }, "Delete") : null;

  node.replaceChildren(
    preview,
    el("div", { class: "insp-name" }, el("strong", { title: name }, name), status),
    path ? el("div", { class: "mono insp-path insp-fullpath" }, path) : null,
    facts,
    why,
    el("div", { class: "insp-actions" }, keepBtn, delBtn),
  );
}

// Shows the card of one unit in `node`, fetched from GET /api/review/unit/{id}. Arrow-key
// bursts fetch only where the selection rests; recent cards are cached. `getDecision` reads
// the caller's (freshest) decision at render time.
export function createInspectorLoader(node, { delayMs = 120, cacheSize = 60, onError } = {}) {
  const cache = new Map();
  let timer = null;
  let wanted = null;

  function show(unitId, getDecision, onDecide) {
    clearTimeout(timer);
    wanted = unitId;
    if (!unitId) { renderInspector(node, null); return; }
    const render = (card) => renderInspector(node, { ...card, decision: getDecision() }, { onDecide });
    if (cache.has(unitId)) { render(cache.get(unitId)); return; }
    timer = setTimeout(async () => {
      try {
        const card = await api("GET", `/api/review/unit/${encodeURIComponent(unitId)}`);
        cache.set(unitId, card);
        if (cache.size > cacheSize) cache.delete(cache.keys().next().value);
        if (wanted === unitId) render(card);
      } catch (e) { if (onError) onError(e); }
    }, delayMs);
  }

  return { show };
}
