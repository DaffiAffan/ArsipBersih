import { api, el } from "/static/core/api.js";
import { fmtBytes, fmtNum } from "/static/core/format.js";
import { watchJob } from "/static/components/jobbar.js";
import { confirmDialog, modalDialog } from "/static/components/dialog.js";
import { reviewNav } from "/static/components/sidebar.js";

const $ = (id) => document.getElementById(id);
const checked = new Set();
let data = null;
let tidyJobSeen = false;
let openMenu = null;

function showError(msg) {
  const box = $("plan-error");
  box.hidden = !msg;
  box.textContent = msg || "";
}

function stat(label, value, sub) {
  return el("li", {}, el("span", {}, label),
    el("span", { class: "list-meta num" }, sub ? `${value} · ${sub}` : value));
}

function summaryStats(s) {
  const stats = [stat("Ke Arsip", `${fmtNum(s.archive_units)} unit`, fmtBytes(s.archive_bytes))];
  for (const [cat, [units, bytes]] of Object.entries(s.review)) {
    stats.push(stat(`Ke Perlu Dicek: ${cat}`, `${fmtNum(units)} unit`, fmtBytes(bytes)));
  }
  for (const [reason, [units, bytes]] of Object.entries(s.by_reason || {})) {
    stats.push(stat(`Di Arsip, ${reason}`, `${fmtNum(units)} unit`, fmtBytes(bytes)));
  }
  stats.push(stat("Dilewati", fmtNum(s.skipped.length)));
  return stats;
}

function summaryBlock(s) {
  const nodes = [el("ul", { class: "list plan-stats" }, summaryStats(s))];
  if (s.skipped.length) {
    nodes.push(el("details", {}, el("summary", {}, "Unit dilewati"),
      el("ul", {}, s.skipped.map(([id, why]) => el("li", {}, `${id}: ${why}`)))));
  }
  nodes.push(el("p", { class: s.space_ok ? "muted" : "plan-error" },
    s.space_ok ? "Ruang disk: cukup" : `Ruang disk: tidak cukup (${s.space_error})`));
  return nodes;
}

function confirmLines(s) {
  const lines = [`Ke Arsip: ${fmtNum(s.archive_units)} unit, ${fmtBytes(s.archive_bytes)}`];
  for (const [cat, [units, bytes]] of Object.entries(s.review)) {
    lines.push(`Ke _review/${cat}: ${fmtNum(units)} unit, ${fmtBytes(bytes)}`);
  }
  for (const [reason, [units, bytes]] of Object.entries(s.by_reason || {})) {
    lines.push(`Di Arsip, ${reason}: ${fmtNum(units)} unit, ${fmtBytes(bytes)}`);
  }
  lines.push(`Unit dilewati: ${fmtNum(s.skipped.length)}`);
  lines.push("Semua perpindahan dicatat dan bisa dibatalkan lewat Riwayat.");
  return lines;
}

async function override(kind, name, value) {
  showError("");
  try {
    data = await api("POST", "/api/events/override", { kind, name, value });
    checked.clear();
    render();
  } catch (e) { showError(e.message); }
}

// Modal with one field (text or select); built on the shared modalDialog in components/dialog.js.
function fieldDialog({ title, lead, field, okLabel }) {
  return modalDialog({
    title, okLabel, value: () => field.value, cancelValue: null, focus: field, submitOnEnter: field,
    nodes: [lead ? el("p", { class: "muted" }, lead) : null, field].filter(Boolean),
  });
}

async function renameEvent(ev) {
  const field = el("input", { type: "text", class: "input", value: ev.name, "aria-label": "Nama baru" });
  const v = await fieldDialog({ title: "Ganti nama acara", lead: `Nama baru untuk "${ev.name}":`, field, okLabel: "Ganti nama" });
  if (!v || !v.trim()) return null;
  await override("rename", ev.name, v.trim());
  return v.trim(); // new name: the row key changes with it, so focus is restored by name
}

async function mergeEvent(ev) {
  const others = data.events.filter((o) => o.key !== ev.key);
  if (!others.length) { showError("Tidak ada acara lain untuk digabung."); return; }
  const field = el("select", { class: "input", "aria-label": "Gabungkan ke" },
    others.map((o) => el("option", { value: o.name }, o.name)));
  const v = await fieldDialog({ title: "Gabungkan ke…", lead: `Acara "${ev.name}" akan digabung ke:`, field, okLabel: "Gabungkan" });
  if (v) await override("merge", ev.name, v);
}

// After a menu action the "…" button (or the whole menu) is gone from the DOM, so focus would
// fall to <body>. Put it back on the row's "…" button, or the first one when the row is gone.
function restoreFocus(key, name = null) {
  if (document.activeElement && document.activeElement !== document.body && document.activeElement.isConnected) return;
  const rows = [...document.querySelectorAll("#events-body .btn-icon")];
  const renamed = name != null ? data.events.find((e) => e.name === name) : null;
  const k = renamed ? renamed.key : key;
  const target = rows.find((b) => b.dataset.key === k) || rows[0] || $("tidy-btn");
  if (target && !target.disabled) target.focus();
}

function closeMenu() {
  if (!openMenu) return;
  const { menu, btn, off } = openMenu;
  openMenu = null;
  off();
  menu.remove();
  btn.setAttribute("aria-expanded", "false");
}

function toggleMenu(btn, ev) {
  const wasOpen = openMenu && openMenu.btn === btn;
  closeMenu();
  if (wasOpen) return;
  const item = (label, fn) => el("button", { type: "button", role: "menuitem", onclick: async () => { closeMenu(); const newName = await fn(); setTimeout(() => restoreFocus(ev.key, typeof newName === "string" ? newName : null), 0); } }, label);
  const menu = el("div", { class: "menu", role: "menu", "aria-label": `Aksi untuk ${ev.name}` },
    item("Ganti nama…", () => renameEvent(ev)),
    item("Gabungkan ke…", () => mergeEvent(ev)),
    item("Bukan acara", () => override("not_event", ev.name)));
  document.body.append(menu);
  const r = btn.getBoundingClientRect();
  menu.style.top = `${window.scrollY + r.bottom + 4}px`;
  menu.style.left = `${Math.max(8, window.scrollX + r.right - menu.offsetWidth)}px`;
  btn.setAttribute("aria-expanded", "true");
  const items = [...menu.querySelectorAll("button")];
  const onKey = (e) => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); closeMenu(); btn.focus(); }
    else if (e.key === "Tab") { closeMenu(); btn.focus(); } // focus continues from the "…" button
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const i = items.indexOf(document.activeElement);
      items[(i + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length].focus();
    }
  };
  // Tab out of the open menu closes it.
  menu.addEventListener("focusout", (e) => {
    if (e.relatedTarget && !menu.contains(e.relatedTarget) && e.relatedTarget !== btn) closeMenu();
  });
  const onDown = (e) => { if (!menu.contains(e.target) && e.target !== btn) closeMenu(); };
  document.addEventListener("keydown", onKey, true);
  document.addEventListener("mousedown", onDown);
  openMenu = { menu, btn, off: () => { document.removeEventListener("keydown", onKey, true); document.removeEventListener("mousedown", onDown); } };
  items[0].focus();
}

function eventRow(ev) {
  const isChecked = checked.has(ev.key);
  const more = el("button", {
    type: "button", class: "btn btn-icon", "aria-haspopup": "menu", "aria-expanded": "false",
    "aria-label": `Aksi lain untuk ${ev.name}`, "data-key": ev.key, onclick: () => toggleMenu(more, ev),
  }, "…");
  return el("tr", { class: isChecked ? "checked" : "" },
    el("td", {}, ev.name, isChecked ? el("span", { class: "badge badge-keep plan-ok" }, "Disetujui") : null),
    el("td", {}, ev.month || "-"),
    el("td", {}, fmtNum(ev.file_count)),
    el("td", {}, (ev.sources || []).join(", ")),
    el("td", { class: "sample" }, ev.sample_path),
    el("td", {}, el("div", { class: "actions" },
      el("button", { type: "button", class: "btn btn-secondary", disabled: isChecked,
        onclick: () => { checked.add(ev.key); render(); } }, isChecked ? "Disetujui" : "Setuju"),
      more)));
}

function render() {
  closeMenu();
  const s = data.summary;
  $("events-body").replaceChildren(...data.events.map(eventRow));
  $("events-empty").hidden = data.events.length > 0;
  $("summary").replaceChildren(...summaryBlock(s));
  const ok = s.scan_done && s.space_ok;
  $("tidy-btn").disabled = !ok;
  $("tidy-hint").textContent = !s.scan_done
    ? "Scan terakhir belum selesai. Jalankan scan lagi dulu."
    : !s.space_ok ? "Ruang disk belum cukup." : "";
}

async function load() {
  showError("");
  try { data = await api("GET", "/api/plan"); render(); } catch (e) { showError(e.message); }
}

$("tidy-btn").addEventListener("click", async () => {
  const ok = await confirmDialog({ title: "Rapikan sekarang?", body: confirmLines(data.summary), okLabel: "Ya, rapikan" });
  if (!ok) return;
  showError("");
  try { await api("POST", "/api/tidy"); tidyJobSeen = true; $("tidy-btn").disabled = true; }
  catch (e) { showError(e.message); }
});

function showResult(st) {
  const r = st.result || {};
  const body = [
    el("p", {}, `Dipindah: ${fmtNum(r.moved_units ?? 0)} unit · gagal: ${fmtNum(r.failed_units ?? 0)} · dilewati: ${fmtNum(r.skipped_units ?? 0)}`),
  ];
  if ((r.errors || []).length) {
    body.push(el("details", {}, el("summary", {}, "Detail"), el("pre", {}, r.errors.join("\n"))));
  }
  body.push(el("p", {}, el("a", { href: "/review" }, "Lanjut ke Review")));
  $("result-body").replaceChildren(...body);
  $("result").hidden = false;
}

watchJob((st) => {
  if (st.kind !== "tidy" || !tidyJobSeen) return;
  if (st.status === "done") { tidyJobSeen = false; showResult(st); load(); reviewNav.refresh(); }
  else if (st.status === "failed") { tidyJobSeen = false; showError(st.error || "Merapikan gagal."); load(); reviewNav.refresh(); }
});

load();
