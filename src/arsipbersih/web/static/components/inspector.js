// Info panel for one unit (shape of GET /api/review/unit/{id}). Used by the grid, the group
// screen and the fullscreen viewer.
import { api, el } from "/static/core/api.js";
import { fmtBytes } from "/static/core/format.js";
import { CATEGORY_LABELS } from "/static/components/labels.js";

const DATE_SOURCES = { filename: "dari nama file", exif: "EXIF", quicktime: "QuickTime" };
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"];
export const DECISION_EFFECT = "Saat Terapkan: Simpan masuk Arsip, Buang masuk Sampah (bisa dibatalkan di Riwayat). Yang belum diputuskan tetap di review.";
export const DISCARD_EFFECT = DECISION_EFFECT; // alias for older callers
// Same names as AI_GROUP_NAMES in classify.py; the order is the menu order.
export const AI_GROUPS = [["identitas", "Identitas"], ["keuangan", "Keuangan"], ["kertas", "Kertas & catatan"],
  ["grafis", "Grafis"], ["layar", "Layar"], ["lainnya", "Foto biasa"]];
// Videos only know three groups; "lainnya" reads "Video biasa" there.
const VIDEO_AI_GROUPS = [["grafis", "Grafis"], ["layar", "Layar"], ["lainnya", "Video biasa"]];

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

// "Tebakan AI: Keuangan · struk (91%)", or "Dikoreksi: Keuangan" after an owner correction.
export function aiGuessText(ai) {
  if (!ai) return "Tebakan AI: belum ada";
  if (ai.source === "koreksi") return `Dikoreksi: ${ai.label}`;
  const sub = ai.sub ? ` · ${ai.sub}` : "";
  const pct = ai.prob != null ? ` (${Math.round(ai.prob * 100)}%)` : "";
  return `Tebakan AI: ${ai.label}${sub}${pct}`;
}

// AI guess + "Ubah kelompok" menu. The button shows only when the caller can save a
// correction. Videos get the three video groups.
function aiSection(unit, onChangeGroup) {
  const isVideo = (unit.media || {}).kind === "video";
  const canChange = Boolean(onChangeGroup);
  if (!unit.ai && !canChange) return null;
  const box = el("section", { class: "insp-ai" }, el("p", { class: "insp-ai-guess" }, aiGuessText(unit.ai)));
  if (!canChange) return box;
  const current = unit.ai && unit.ai.source === "koreksi" ? unit.ai.grp : null;
  const menu = el("div", { class: "insp-ai-menu", role: "menu", "aria-label": "Pilih kelompok", hidden: true });
  const toggle = el("button", { type: "button", class: "btn btn-ghost insp-ai-btn", "aria-haspopup": "menu", "aria-expanded": "false",
    onclick: () => {
      menu.hidden = !menu.hidden;
      toggle.setAttribute("aria-expanded", String(!menu.hidden));
      if (!menu.hidden) { const first = menu.querySelector("button"); if (first) first.focus(); }
    } }, "Ubah kelompok");
  let saving = false;
  for (const [grp, name] of (isVideo ? VIDEO_AI_GROUPS : AI_GROUPS)) {
    menu.append(el("button", { type: "button", class: "insp-ai-item", role: "menuitemradio", "aria-checked": String(grp === current),
      onclick: async () => {
        if (saving) return;
        saving = true;
        menu.hidden = true;
        toggle.setAttribute("aria-expanded", "false");
        toggle.disabled = true;
        try { await onChangeGroup(grp); } finally { saving = false; toggle.disabled = false; }
      } }, name));
  }
  menu.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape") return;
    ev.stopPropagation(); // close the menu only, not the fullscreen viewer
    menu.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
    toggle.focus();
  });
  box.append(el("div", { class: "insp-ai-ctl" }, toggle, menu));
  return box;
}

// "Potongan dari: nama (mulai 0:45)" and "Punya potongan: N" with links that open that unit.
function clipsSection(unit, onOpenUnit) {
  const { parent, children } = unit.clips || {};
  if (!parent && !(children && children.length)) return null;
  const link = (c) => el("button", { type: "button", class: "btn btn-ghost insp-clip-link", disabled: !onOpenUnit,
    onclick: () => onOpenUnit(c.unit_id) }, `${c.name} (mulai ${fmtDuration(c.offset_s)})`);
  const box = el("section", { class: "insp-clips" });
  if (parent) box.append(el("p", {}, "Potongan dari: ", link(parent)));
  if (children && children.length) {
    box.append(el("p", {}, `Punya potongan: ${children.length}`),
      el("ul", {}, ...children.map((c) => el("li", {}, link(c)))));
  }
  return box;
}

// Format = file extension in capitals ("IMG_1.jpg" -> "JPG").
function formatOf(name) {
  const dot = String(name).lastIndexOf(".");
  return dot > 0 && dot < name.length - 1 ? name.slice(dot + 1).toUpperCase() : "";
}

function folderOf(path) {
  const i = path ? path.lastIndexOf("/") : -1;
  return i > 0 ? path.slice(0, i) : "";
}

export function renderInspector(node, unit, { onDecide, onChangeGroup, onOpenUnit } = {}) {
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
    el("p", { class: "insp-effect" }, DECISION_EFFECT));

  const d = unit.decision;
  const decide = (want) => { if (onDecide) onDecide(d === want ? null : want); };
  const keepBtn = el("button", { type: "button", class: "btn btn-keep", "aria-pressed": String(d === "keep"), onclick: () => decide("keep") },
    "Simpan · ", el("kbd", { class: "kbd" }, "S"));
  const delBtn = el("button", { type: "button", class: "btn btn-danger", "aria-pressed": String(d === "discard"), onclick: () => decide("discard") },
    "Buang · ", el("kbd", { class: "kbd" }, "D"));
  const status = d === "keep" ? el("span", { class: "chip chip-keep" }, "Disimpan")
    : d === "discard" ? el("span", { class: "chip chip-discard" }, "Dibuang")
      : unit.seen ? el("span", { class: "chip chip-neutral" }, "Sudah dilihat") : null;

  node.replaceChildren(...[
    preview,
    el("div", { class: "insp-name" }, el("strong", { title: name }, name),
      formatOf(name) ? el("span", { class: "chip chip-neutral insp-format" }, formatOf(name)) : null, status),
    path ? el("div", { class: "mono insp-path insp-fullpath" }, path) : null,
    facts,
    aiSection(unit, onChangeGroup),
    clipsSection(unit, onOpenUnit),
    why,
    onDecide ? el("div", { class: "insp-actions" }, keepBtn, delBtn) : null, // view-only without onDecide
  ].filter(Boolean)); // replaceChildren turns a null into the text "null"
}

// Shows the card of one unit in `node`, fetched from GET /api/review/unit/{id}. Arrow-key
// bursts fetch only where the selection rests; recent cards are cached. `getDecision` reads
// the caller's (freshest) decision at render time. `forget(id)` drops a cached card that changed.
export function createInspectorLoader(node, { delayMs = 120, cacheSize = 60, onError } = {}) {
  const cache = new Map();
  let timer = null;
  let wanted = null;

  function show(unitId, getDecision, onDecide, onChangeGroup) {
    clearTimeout(timer);
    wanted = unitId;
    if (!unitId) { renderInspector(node, null); return; }
    const render = (card) => renderInspector(node, { ...card, decision: getDecision() }, { onDecide, onChangeGroup });
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

  function forget(unitId) { cache.delete(unitId); }

  return { show, forget };
}
