export async function api(method, url, body) {
  const opts = { method, headers: { "X-ArsipBersih": "1" } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  let data = null;
  try { data = await res.json(); } catch (_) { /* empty or non-JSON body */ }
  if (!res.ok) throw new Error((data && data.detail) || `Permintaan gagal (${res.status})`);
  return data;
}

// The same location names everywhere in the UI (folder names on disk).
export const LOCATION_LABELS = { source: "Sumber", archive: "Arsip", review: "_review", trash: "_sampah" };

export function fmtBytes(n) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = Number(n) || 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  const s = i === 0 ? String(Math.round(v)) : v.toFixed(1).replace(".", ",");
  return `${s} ${units[i]}`;
}

export function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (k === "class") node.className = v;
    else if (v !== false && v != null) node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(c.nodeType ? c : document.createTextNode(String(c)));
  }
  return node;
}
