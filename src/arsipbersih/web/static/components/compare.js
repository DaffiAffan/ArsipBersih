// Large side-by-side comparison of one group (shape of GET /api/review/group/{id}). Up to three
// members fit next to each other; more scroll horizontally. Photos use `contain` on a near-black
// stage. D-038: before a decision every photo stays at full brightness and the suggestion is only
// a label; after a decision the discarded photo is dimmed and marked ✕, the kept one ✓.
import { el, LOCATION_LABELS } from "/static/core/api.js";
import { fmtBytes } from "/static/core/format.js";
import { createZoom } from "/static/components/zoom.js";
import { onClick } from "/static/core/keys.js";

// A member can be decided unless its decision is already applied or it sits in the trash.
export function locked(m) { return Boolean(m.applied) || m.location === "trash"; }

function ext(name) {
  const m = /\.([A-Za-z0-9]+)$/.exec(name || "");
  return m ? m[1].toUpperCase() : "";
}

const pixels = (m) => (m.primary && m.primary.width && m.primary.height ? m.primary.width * m.primary.height : 0);
const bytes = (m) => (m.primary && m.primary.size_total) || 0;

// Index set of the members holding the group's best value; empty when all are equal.
function best(members, value) {
  const vals = members.map(value);
  const top = Math.max(...vals);
  if (!top || vals.every((v) => v === top)) return new Set();
  return new Set(vals.flatMap((v, i) => (v === top ? [i] : [])));
}

function row(label, ...value) {
  return [el("dt", {}, label), el("dd", {}, ...value)];
}

function chipOf(m) {
  if (m.decision === "keep") return el("span", { class: "chip chip-keep" }, "✓ Disimpan");
  if (m.decision === "discard") return el("span", { class: "chip chip-discard" }, "✕ Dibuang");
  return m.is_keeper ? el("span", { class: "chip chip-suggest" }, "Saran: simpan")
    : el("span", { class: "chip chip-neutral" }, "Saran: buang");
}

// onDecide(unitId, "keep"|"discard"|null); onActivate(index) when a photo is clicked;
// onAccept() "Ikuti saran"; onClear() "Batalkan keputusan". Returns {paint, setActive,
// toggleZoom, destroy}: toggleZoom() turns "Zoom bareng" on (true) / off (false), null = no photo.
export function renderCompare(container, group, { onDecide, onActivate, onAccept, onClear, activeIndex = 0 }) {
  const members = group.members;
  const bigRes = best(members, pixels);
  const bigSize = best(members, bytes);
  let active = activeIndex;

  const acceptBtn = el("button", { type: "button", class: "btn btn-primary", onclick: onClick(() => onAccept && onAccept()) },
    "Ikuti saran ", el("kbd", { class: "kbd" }, "Enter"));
  const clearBtn = el("button", { type: "button", class: "btn", onclick: onClick(() => onClear && onClear()) }, "Batalkan keputusan");
  const head = el("div", { class: "cmp-head" },
    el("span", { class: "cmp-summary" }, [group.summary, group.folder].filter(Boolean).join(" · ")),
    el("span", { class: "toolbar-spacer" }), acceptBtn, clearBtn);

  const cards = members.map((m, i) => {
    const p = m.primary || {};
    const media = m.media || {};
    const src = media.kind === "video" ? media.thumb : (media.view || media.thumb);
    const name = p.name || "-";
    const pane = el("div", { class: "cmp-pane" },
      el("img", { src: src || "", alt: name, draggable: "false" }));
    const mark = el("span", { class: "cmp-mark", "aria-hidden": "true" });
    const chipSlot = el("dd", { class: "cmp-chips" });
    const keepBtn = el("button", { type: "button", class: "btn", onclick: onClick(() => onDecide(m.unit_id, m.decision === "keep" ? null : "keep")) },
      "Simpan ", el("kbd", { class: "kbd" }, "S"));
    const delBtn = el("button", { type: "button", class: "btn", onclick: onClick(() => onDecide(m.unit_id, m.decision === "discard" ? null : "discard")) },
      "Buang ", el("kbd", { class: "kbd" }, "D"));
    const btns = el("div", { class: "cmp-btns" }, keepBtn, delBtn);
    const size = p.size_total != null ? el("span", { class: bigSize.has(i) ? "is-better" : "" }, fmtBytes(p.size_total)) : null;
    const res = p.width && p.height ? el("span", { class: bigRes.has(i) ? "is-better" : "" }, `${p.width} × ${p.height}`) : null;
    const origin = [p.source_label, m.origin, LOCATION_LABELS[m.location] || m.location].filter(Boolean).join(" · ");
    const node = el("article", { class: "cmp-photo", "data-id": m.unit_id },
      el("div", { class: "cmp-stage" }, pane, mark),
      el("div", { class: "cmp-name" }, el("strong", { title: name }, name), ext(name) ? el("span", { class: "chip chip-neutral" }, ext(name)) : null),
      el("dl", { class: "cmp-meta" },
        el("dt", {}, "Status"), chipSlot,
        ...row("Ukuran", ...[size, size && res ? " · " : null, res].filter(Boolean)),
        ...row("Asal", origin || "-"),
        // Same rows on every photo so the photos line up; the reason belongs to the keeper only.
        ...(group.reason ? row(m.is_keeper ? "Alasan saran" : " ", m.is_keeper ? group.reason : " ") : [])),
      btns);
    pane.addEventListener("click", () => { if (onActivate) onActivate(i); });
    function paint() {
      const lock = locked(m);
      node.classList.toggle("is-active", i === active);
      node.classList.toggle("is-keep", m.decision === "keep");
      node.classList.toggle("is-discard", m.decision === "discard");
      mark.textContent = m.decision === "keep" ? "✓" : m.decision === "discard" ? "✕" : "";
      chipSlot.replaceChildren(...[chipOf(m),
        lock ? el("span", { class: "chip chip-neutral" }, m.location === "trash" ? "Sudah di Sampah" : "Sudah diproses") : null].filter(Boolean));
      btns.hidden = lock;
      keepBtn.setAttribute("aria-pressed", String(m.decision === "keep"));
      delBtn.setAttribute("aria-pressed", String(m.decision === "discard"));
      node.setAttribute("aria-label", `${name}, ${m.decision === "keep" ? "disimpan" : m.decision === "discard" ? "dibuang" : "belum diputuskan"}`);
    }
    return { node, pane, paint };
  });

  const rowEl = el("div", { class: "cmp-row", style: `--cmp-cols: ${Math.min(3, Math.max(1, members.length))}` },
    ...cards.map((c) => c.node));
  container.replaceChildren(el("section", { class: "cmp", "aria-label": "Perbandingan" }, head, rowEl));

  function paint() {
    const open = members.filter((m) => !locked(m));
    acceptBtn.hidden = !open.some((m) => m.decision == null);
    clearBtn.hidden = !open.some((m) => m.decision != null);
    cards.forEach((c) => c.paint());
  }

  function setActive(i) {
    if (i < 0 || i >= cards.length) return;
    active = i;
    cards.forEach((c) => c.paint());
    const node = cards[i].node;
    if (node.scrollIntoView) node.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  let zoom = null;
  function toggleZoom() {
    if (zoom) { zoom.destroy(); zoom = null; return false; }
    const panes = cards.filter((_, i) => (members[i].media || {}).kind !== "video").map((c) => c.pane);
    if (!panes.length) return null;
    zoom = createZoom(panes);
    return true;
  }

  paint();
  return { paint, setActive, toggleZoom, destroy: () => { if (zoom) { zoom.destroy(); zoom = null; } },
    get active() { return active; } };
}
