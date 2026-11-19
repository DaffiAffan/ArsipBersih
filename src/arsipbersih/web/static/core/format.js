export function fmtBytes(n) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = Number(n) || 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  const s = i === 0 ? String(Math.round(v)) : v.toFixed(1).replace(".", ",");
  return `${s} ${units[i]}`;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"];

export function fmtNum(n) {
  return new Intl.NumberFormat("id-ID").format(Number(n) || 0);
}

function hm(d) {
  return d.toLocaleTimeString("id-ID", { hour: "2-digit", minute: "2-digit", hour12: false }).replace(":", ".");
}

// "baru saja", "N menit lalu", "hari ini, 14.05", "kemarin, 22.12", "16 Nov 2026, 15.45".
export function fmtTime(iso, now = new Date()) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso);
  const diff = now.getTime() - d.getTime();
  if (diff >= 0 && diff < 60000) return "baru saja";
  if (diff >= 0 && diff < 3600000) return `${Math.floor(diff / 60000)} menit lalu`;
  const startOf = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((startOf(now) - startOf(d)) / 86400000);
  if (days === 0) return `hari ini, ${hm(d)}`;
  if (days === 1) return `kemarin, ${hm(d)}`;
  return `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}, ${hm(d)}`;
}
