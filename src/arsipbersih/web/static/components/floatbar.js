// Floating Apply bar: "N Keep · M ke _sampah" (grid) or "N dari M grup diputuskan" (group).
// Apply asks for confirmation with the server's preview text and sends the counts the owner
// saw; the server answers 409 when they changed in between (D-024).
import { api, el } from "/static/core/api.js";
import { watchJob } from "/static/components/jobbar.js";
import { reviewNav } from "/static/components/sidebar.js";
import { confirmDialog } from "/static/components/dialog.js";
import { fmtNum } from "/static/core/format.js";
import { toast } from "/static/components/toast.js";

function count(v) { return Array.isArray(v) ? v.length : Number(v) || 0; }

export function mountFloatBar(container, { category, mode, label = category, onApplied, beforeApply }) {
  const summary = el("span", { class: "floatbar-summary" }, "…");
  const applyBtn = el("button", { type: "button", class: "btn btn-primary", disabled: true }, "Apply");
  const bar = el("div", { class: "floatbar", role: "region", "aria-label": "Apply" }, summary, applyBtn);
  container.append(bar);

  let applying = false;
  let pending = 0;
  let inflight = null;
  let again = false;

  function paint() {
    applyBtn.disabled = applying || pending === 0;
  }

  async function load() {
    if (mode === "group") {
      const cats = await reviewNav.refresh(); // shared with the sidebar: one request
      if (!cats) throw new Error("categories unavailable");
      const mine = cats.find((c) => c.category === category);
      const done = mine ? mine.done : 0;
      const total = mine ? mine.count : 0;
      pending = mine ? mine.pending_apply : 0;
      summary.textContent = `${fmtNum(done)} dari ${fmtNum(total)} grup diputuskan`;
    } else {
      const p = await api("GET", `/api/review/apply/${encodeURIComponent(category)}`);
      pending = p.keep + p.discard;
      summary.replaceChildren(
        el("span", { class: "floatbar-keep" }, `${fmtNum(p.keep)} Keep`), " · ",
        el("span", { class: "floatbar-trash" }, `${fmtNum(p.discard)} ke _sampah`));
    }
    paint();
  }

  // Coalesces bursts of calls (every decision and seen flush asks for a refresh).
  async function refresh() {
    if (inflight) { again = true; return inflight; }
    inflight = (async () => {
      do {
        again = false;
        try { await load(); } catch (_) { /* keep the previous numbers */ }
      } while (again);
    })();
    try { await inflight; } finally { inflight = null; }
  }

  function finish(st, startedId) {
    applying = false;
    applyBtn.textContent = "Apply";
    paint();
    if (st.status === "done" && st.result) {
      const r = st.result;
      const failed = count(r.failed_units);
      toast(`${fmtNum(count(r.moved_units))} dipindah · ${fmtNum(failed)} gagal · ${fmtNum(count(r.skipped_units))} dilewati`,
        { kind: failed ? "error" : "ok" });
    } else {
      toast(`Apply gagal: ${st.error || st.status}`, { kind: "error" });
    }
    if (onApplied) onApplied(startedId);
    reviewNav.refresh();
    refresh();
  }

  async function apply() {
    if (applying) return;
    applying = true;
    paint();
    let jobRunning = false;
    try {
      if (beforeApply && !(await beforeApply())) return;
      let preview;
      try { preview = await api("GET", `/api/review/apply/${encodeURIComponent(category)}`); } catch (e) {
        toast(e.message, { kind: "error" });
        return;
      }
      if (preview.keep + preview.discard === 0) { toast("Belum ada yang bisa di-Apply."); return; }
      const ok = await confirmDialog({
        title: `Apply: ${label}`,
        body: [preview.text, "File yang di-delete masuk _sampah dan masih bisa di-Undo di Riwayat."],
        okLabel: "Apply",
      });
      if (!ok) return;
      let started;
      try {
        started = await api("POST", `/api/review/apply/${encodeURIComponent(category)}`,
          { keep: preview.keep, discard: preview.discard, fingerprint: preview.fingerprint });
      } catch (e) {
        toast(e.message, { kind: "error" }); // 409: counts changed since the dialog (D-024)
        refresh();
        return;
      }
      jobRunning = true;
      applyBtn.textContent = "Memindahkan file…";
      let finished = false;
      const stop = watchJob((st) => {
        if (finished || st.id !== started.id || st.status === "running" || st.status === "idle") return;
        finished = true;
        setTimeout(() => stop(), 0);
        finish(st, started.id);
      });
      return; // stays "applying" until the job ends
    } finally {
      if (!jobRunning) { applying = false; paint(); }
    }
  }

  applyBtn.addEventListener("click", apply);
  refresh();
  return { refresh, get applying() { return applying; } };
}
