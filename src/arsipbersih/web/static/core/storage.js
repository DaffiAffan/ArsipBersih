// Browser storage that never throws: "local" survives the browser, "session" lives per tab.
// Values are JSON; a blocked, full or corrupt store reads as `fallback` and writes are skipped.
const area = (store) => (store === "session" ? sessionStorage : localStorage);

export function load(store, key, fallback = null) {
  try {
    const raw = area(store).getItem(key);
    return raw == null ? fallback : JSON.parse(raw);
  } catch (_) { return fallback; }
}

export function save(store, key, value) {
  try { area(store).setItem(key, JSON.stringify(value)); } catch (_) { /* best effort */ }
}
