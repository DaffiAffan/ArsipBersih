import { el } from "./api.js";
import { bindKeys } from "./keys.js";
import { renderInspector } from "./inspector.js";
import { createZoom } from "./zoom.js";
import { toast, pushHelp } from "./ui.js";

export { CATEGORY_LABELS } from "./labels.js";

function preload(item) {
  if (item && item.media && item.media.kind !== "video" && item.media.view) new Image().src = item.media.view;
}

// `onLoaded()` fires once the full media is really on screen: the full-size image decoded
// (not the grid thumbnail a placeholder card carries) or the video's metadata loaded.
// Nothing fires when the media fails to load.
function mediaPane(item, onLoaded) {
  const pane = el("div", { class: "vw-pane" });
  const m = item.media || {};
  if (m.kind === "video" && m.video) {
    const status = el("div", { class: "vw-status" }, "Menyiapkan video…");
    const video = el("video", { controls: true, preload: "auto", src: m.video });
    video.addEventListener("loadedmetadata", () => onLoaded(), { once: true });
    video.addEventListener("canplay", () => status.remove());
    video.addEventListener("error", () => {
      status.textContent = "Video tidak bisa diputar. Format atau file mungkin tidak didukung browser.";
      status.classList.add("vw-error");
    });
    pane.append(video, status);
    pane.isVideo = true;
  } else {
    const img = el("img", { src: m.view || m.thumb, alt: item.primary ? item.primary.name : "", draggable: "false" });
    if (m.view) {
      const loaded = () => { if (img.naturalWidth > 1 || img.naturalHeight > 1) onLoaded(); };
      if (img.complete && img.naturalWidth) queueMicrotask(loaded);
      else img.addEventListener("load", loaded, { once: true });
    }
    pane.append(img);
  }
  return pane;
}

// `onChangeGroup(item, grp)` (optional, single mode): the owner corrects the AI group from
// the info panel; the caller updates `items` and calls go() when the card changes.
export function openViewer({ mode = "single", items, index = 0, onDecide, onIndex, onClose, onShown, onChangeGroup }) {
  if (!items || !items.length) return { close() {}, go() {}, index: 0 };
  let i = Math.min(Math.max(index, 0), items.length - 1);
  let zoom = null;
  const overlay = el("div", { class: "vw-overlay", role: "dialog", "aria-modal": "true" });
  const stage = el("div", { class: "vw-stage" });
  const side = el("aside", { class: "vw-side inspector", "aria-label": "Info foto" });
  const counter = el("span", { class: "vw-counter", "aria-live": "polite" });
  const hint = el("span", { class: "vw-hint" });
  const hasVideo = items.flat().some((it) => it && it.media && it.media.kind === "video");
  const hasImage = items.flat().some((it) => it && !(it.media && it.media.kind === "video"));
  const hintKeys = [["← →", "pindah"], ["S", "keep"], ["D", "delete"], ["Enter", "ikuti saran"],
    ...(hasVideo ? [["Spasi", "play/pause"]] : []), ["Esc", "tutup"]];
  for (const [k, t] of hintKeys) {
    if (hint.childNodes.length) hint.append(" · ");
    hint.append(el("kbd", { class: "kbd" }, k), ` ${t}`);
  }
  const bar = el("div", { class: "vw-bar" }, counter, hint,
    el("button", { type: "button", class: "btn", onclick: () => close() }, "Tutup ", el("kbd", { class: "kbd" }, "Esc")));
  overlay.append(bar, el("div", { class: "vw-body" }, stage, side));
  document.body.append(overlay);

  // compare: items[i] = [kiri, kanan]; item aktif = panel kanan.
  function current() { return mode === "compare" ? items[i][1] : items[i]; }

  // Inspector panel(s). A decision made with the panel's own buttons goes through the same
  // onDecide as the S/D keys; null (toggle off) means "back to the suggestion".
  function renderSide() {
    const shown = mode === "compare" ? items[i] : [items[i]];
    // replaceChildren drops the focused Keep/Delete button; remember which one to refocus.
    const had = document.activeElement && side.contains(document.activeElement) ? document.activeElement : null;
    const hadUnit = had ? [...side.querySelectorAll(".vw-unit")].findIndex((u) => u.contains(had)) : -1;
    const hadCls = had && had.classList.contains("btn-danger") ? ".btn-danger" : ".btn-keep";
    side.replaceChildren();
    shown.forEach((it, n) => {
      const box = el("div", { class: "vw-unit" });
      if (mode === "compare") box.append(el("h3", {}, n === 0 ? "Kiri" : "Kanan (aktif)"));
      const body = el("div", {});
      box.append(body);
      side.append(box);
      renderInspector(body, it, {
        onDecide: (want) => safeDecide(it, want === null ? "suggest" : want),
        onChangeGroup: onChangeGroup && mode !== "compare" ? (grp) => safeChangeGroup(it, grp) : null,
      });
    });
    if (had) {
      const u = side.querySelectorAll(".vw-unit")[Math.max(hadUnit, 0)];
      const b = u && u.querySelector(hadCls);
      if (b) b.focus();
    }
  }

  function render() {
    if (zoom) zoom.destroy();
    stage.replaceChildren();
    const shown = mode === "compare" ? items[i] : [items[i]];
    // onShown(item): that item's full media loaded while it is still the one on screen.
    const panes = shown.map((it) => {
      const pane = mediaPane(it, () => {
        if (onShown && !closed && pane.isConnected && shown.includes(it)) onShown(it);
      });
      return pane;
    });
    panes.forEach((p) => stage.append(p));
    const zoomable = panes.filter((p) => !p.isVideo);
    zoom = zoomable.length ? createZoom(zoomable) : null;
    renderSide();
    counter.textContent = `${i + 1} / ${items.length}`;
    for (const j of [i + 1, i - 1]) {
      const x = items[j];
      if (x) (mode === "compare" ? x : [x]).forEach(preload);
    }
  }

  // Every decision path (S/D/Enter and the inspector buttons) goes through here: wait for the
  // owner's onDecide, and only advance (Enter) once it reports the decision as saved.
  async function safeDecide(item, kind, advance = false) {
    if (!onDecide) return false;
    const at = i;
    let ok = false;
    try { ok = Boolean(await onDecide(item, kind)); }
    catch (e) { toast(e && e.message ? e.message : "Keputusan gagal disimpan.", { kind: "error" }); }
    if (closed || i !== at) return ok;
    if (ok && advance && i + 1 < items.length) go(i + 1);
    else renderSide();
    return ok;
  }

  async function safeChangeGroup(item, grp) {
    try { await onChangeGroup(item, grp); }
    catch (e) { toast(e && e.message ? e.message : "Kelompok gagal disimpan.", { kind: "error" }); }
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
    s: () => { safeDecide(current(), "keep"); },
    d: (e) => { if (e && e.shiftKey) return; safeDecide(current(), "discard"); }, // Shift+D is a grid-only bulk action
    Enter: () => { safeDecide(current(), "suggest", true); },
    Escape: () => close(),
    " ": (e) => {
      if (e && e.target && e.target.closest && e.target.closest("button")) return;
      const v = stage.querySelector("video");
      if (v) { if (v.paused) v.play(); else v.pause(); }
    },
  });

  // Viewer keys for the "?" dialog (the page's own sections stay below).
  const unhelp = pushHelp([{ title: "Layar penuh", keys: [
    ["← →", "Foto sebelumnya / berikutnya"], ["S", "Keep foto ini"], ["D", "Delete foto ini"],
    ["Enter", "Ikuti saran, lalu ke foto berikutnya"],
    ...(hasVideo ? [["Spasi", "Putar / jeda video"]] : []),
    ...(hasImage ? [["Roda mouse", "Zoom foto"], ["Seret", "Geser foto yang di-zoom"], ["Klik ganda", "Kembalikan zoom"]] : []),
    ["Esc", "Tutup layar penuh"], ["?", "Bantuan ini"]] }]);

  let closed = false;
  function close() {
    if (closed) return;
    closed = true;
    unhelp();
    unbind();
    if (zoom) zoom.destroy();
    overlay.remove();
    if (onClose) onClose();
  }

  render();
  return { close, go, get index() { return i; } };
}
