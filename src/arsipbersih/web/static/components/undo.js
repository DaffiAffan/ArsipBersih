// "Batalkan" toast after a bulk decision that moves no files (Shift+D, Ikuti saran; D-036).
import { api } from "/static/core/api.js";
import { toast } from "/static/components/toast.js";

const CLEAR_MAX = 500; // POST /api/review/decisions/clear limit

// Removes the unapplied decisions of `ids`; resolves to how many the server cleared.
// `onChunk(part)` runs after each chunk the server confirmed, so the caller's local state matches
// what was really cleared even when a later chunk fails.
export async function clearDecisions(ids, onChunk = null) {
  let cleared = 0;
  for (let at = 0; at < ids.length; at += CLEAR_MAX) {
    const part = ids.slice(at, at + CLEAR_MAX);
    cleared += (await api("POST", "/api/review/decisions/clear", { unit_ids: part })).cleared;
    if (onChunk) onChunk(part);
  }
  return cleared;
}

// Shows `text` with a Batalkan button (none when `ids` is empty). On click: when `busyText()`
// returns a reason the toast comes back with it instead of dropping the undo; otherwise
// `run(stillIds(ids))` gets only the ids that still hold the bulk decision, so a choice the
// owner changed afterwards survives.
export function offerUndo(text, ids, opts) {
  const { busyText, stillIds, run } = opts;
  toast(text, ids.length ? { action: { label: "Batalkan", onClick: () => {
    const why = busyText();
    if (why) { offerUndo(why, ids, opts); return; }
    const still = stillIds(ids);
    if (still.length) run(still);
    else toast("Tidak ada yang perlu dibatalkan");
  } } } : {});
}
