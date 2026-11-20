// One grid thumbnail with its four states (D-037..D-039): not seen (plain), seen (small dot),
// kept (✓) and discarded (dimmed + ✕). The state is also in aria-label, never only a colour.
import { el } from "/static/core/api.js";

const STATUS = { keep: "disimpan", discard: "dibuang" };

function ext(name) {
  const m = /\.([A-Za-z0-9]+)$/.exec(name || "");
  return m ? m[1].toUpperCase() : "";
}

function statusText(item) {
  return STATUS[item.decision] || (item.seen ? "sudah dilihat" : "belum dilihat");
}

// Returns the cell; `item.thumb` is the image URL ("" when there is no preview).
export function renderThumb(item, { caption = false } = {}) {
  const name = item.name || "";
  const node = el("div", { class: "thumb", tabindex: "-1", role: "img", "data-id": item.unit_id },
    el("img", { src: item.thumb || "", alt: "", loading: "lazy", draggable: "false" }),
    el("span", { class: "thumb-noprev", hidden: true }, "Pratinjau tidak tersedia"),
    el("span", { class: "thumb-mark", "aria-hidden": "true" }),
    el("span", { class: "thumb-caption", "aria-hidden": "true" },
      el("span", { class: "thumb-name", title: name }, name),
      el("span", { class: "thumb-format" }, ext(name))));
  updateThumb(node, item, { caption });
  return node;
}

// Repaints the state classes, the mark and the caption without rebuilding the cell.
export function updateThumb(node, item, { caption = false, focused = false } = {}) {
  const keep = item.decision === "keep";
  const discard = item.decision === "discard";
  node.classList.toggle("is-keep", keep);
  node.classList.toggle("is-discard", discard);
  node.classList.toggle("is-seen", Boolean(item.seen) && !item.decision);
  node.classList.toggle("is-focused", focused);
  node.querySelector(".thumb-mark").textContent = keep ? "✓" : discard ? "✕" : "";
  node.querySelector(".thumb-caption").hidden = !caption;
  node.setAttribute("aria-label", `${item.name || "Foto"}, ${statusText(item)}`);
}
