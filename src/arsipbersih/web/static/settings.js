import { api, el } from "/static/api.js";

const NOTE = "perlu dicek pada data asli";
const GROUPS = [
  { title: "Kualitas foto", fields: [
    ["blur_fallback", "Ambang burem cadangan", true],
    ["kepencet_sharpness_max", "Kepencet: ketajaman maks", true],
    ["kepencet_luma_max", "Kepencet: kecerahan maks", true],
    ["kepencet_std_max", "Kepencet: variasi maks", true],
    ["dark_luma_max", "Terlalu gelap: kecerahan maks", true],
    ["bright_luma_min", "Terlalu terang: kecerahan min", true],
    ["clipping_max", "Piksel terpotong maks (0-1)", true],
  ] },
  { title: "Duplikat & mirip", fields: [
    ["phash_version_max", "Jarak maks versi lain (0-64)", false],
    ["phash_burst_max", "Jarak maks jepretan beruntun (0-64)", false],
    ["burst_window_s", "Jendela jepretan beruntun (detik)", false],
  ] },
  { title: "Video", fields: [
    ["video_frame_phash_max", "Jarak maks per frame (0-64)", true],
    ["short_video_s", "Video pendek (detik)", false],
    ["live_photo_mov_max_s", "Maks durasi Live Photo (detik)", false],
    ["dark_video_luma_max", "Video gelap: kecerahan maks", true],
  ] },
];
const form = document.getElementById("settings-form");
const msg = document.getElementById("settings-msg");
const inputs = {};
let folders, tz, workers, slider, sliderVal, examplesBox;
let blurValue = null;

function say(text, ok = true) {
  msg.textContent = text;
  msg.style.color = ok ? "" : "var(--danger)";
}

function numField([name, label, claude]) {
  const input = el("input", { type: "number", step: "any", name });
  inputs[name] = input;
  return el("label", { class: "field" }, el("span", {}, label),
    input, claude ? el("small", { class: "muted" }, NOTE) : null);
}

function thumbRow(title, items) {
  const row = el("div", { class: "blur-row" });
  if (!items.length) row.append(el("span", { class: "muted" }, "Belum ada contoh."));
  for (const it of items) {
    row.append(el("figure", {}, el("img", { src: it.thumb, alt: "", loading: "lazy", width: 96 }),
      el("figcaption", { class: "muted" }, Number(it.score).toFixed(1))));
  }
  return el("div", {}, el("h3", {}, title), row);
}

let timer = null;
function scheduleExamples() {
  clearTimeout(timer);
  timer = setTimeout(loadExamples, 300);
}
async function loadExamples() {
  try {
    const d = await api("GET", `/api/settings/blur-examples?threshold=${slider.value}`);
    examplesBox.replaceChildren(
      thumbRow("Di bawah ambang (dianggap burem)", d.below),
      thumbRow("Di atas ambang (dianggap tajam)", d.above));
  } catch (e) { say(e.message, false); }
}

function buildBlur() {
  slider = el("input", { type: "range", min: 0, max: 100, step: 0.5, value: 25 });
  sliderVal = el("output", {}, "");
  slider.addEventListener("input", () => {
    blurValue = Number(slider.value);
    sliderVal.textContent = slider.value;
    scheduleExamples();
  });
  const clearBtn = el("button", { type: "button", onclick: () => {
    blurValue = null;
    sliderVal.textContent = "otomatis";
    scheduleExamples();
  } }, "Pakai ambang cadangan");
  const suggest = el("button", { type: "button", onclick: async () => {
    try {
      const d = await api("POST", "/api/settings/calibrate");
      slider.value = d.suggested;
      slider.dispatchEvent(new Event("input"));
      say(`Saran ambang: ${d.suggested.toFixed(1)}. Klik Simpan untuk memakainya.`);
    } catch (e) { say(e.message, false); }
  } }, "Sarankan dari foto saya");
  examplesBox = el("div", { class: "blur-examples" });
  return el("fieldset", {}, el("legend", {}, "Ambang burem"),
    el("small", { class: "muted" }, `Ambang ini ${NOTE}.`),
    el("div", { class: "blur-controls" }, slider, sliderVal, clearBtn, suggest), examplesBox);
}

function build() {
  const blur = buildBlur();
  const first = GROUPS[0];
  form.append(el("fieldset", {}, el("legend", {}, first.title), first.fields.map(numField)));
  form.append(blur);
  for (const g of GROUPS.slice(1)) {
    form.append(el("fieldset", {}, el("legend", {}, g.title), g.fields.map(numField)));
  }
  folders = el("textarea", { rows: 10, name: "generic_folders" });
  form.append(el("fieldset", {}, el("legend", {}, "Acara"),
    el("label", { class: "field" }, el("span", {}, "Folder bawaan (satu nama per baris)"), folders)));
  tz = el("input", { type: "text", name: "timezone" });
  workers = el("input", { type: "number", min: 1, max: 8, step: 1, name: "workers" });
  form.append(el("fieldset", {}, el("legend", {}, "Umum"),
    el("label", { class: "field" }, el("span", {}, "Zona waktu"), tz),
    el("label", { class: "field" }, el("span", {}, "Jumlah worker (1-8)"), workers)));
}

function fill(s) {
  for (const [k, input] of Object.entries(inputs)) input.value = s[k];
  folders.value = s.generic_folders.join("\n");
  tz.value = s.timezone;
  workers.value = s.workers;
  blurValue = s.blur_threshold;
  slider.value = s.blur_effective;
  sliderVal.textContent = s.blur_threshold == null ? `otomatis (${Number(s.blur_effective).toFixed(1)})` : String(s.blur_threshold);
  loadExamples();
}

async function save() {
  const body = { blur_threshold: blurValue, timezone: tz.value, workers: Number(workers.value),
    generic_folders: folders.value.split("\n") };
  for (const [k, input] of Object.entries(inputs)) body[k] = input.value === "" ? null : Number(input.value);
  try {
    const d = await api("PUT", "/api/settings", body);
    fill(d);
    if (d.rescan_needed) {
      msg.style.color = "";
      msg.replaceChildren("Tersimpan. Pindai ulang supaya pengaturan baru berlaku. ", el("a", { href: "/" }, "Ke Beranda"));
    } else say("Tersimpan.");
  } catch (e) { say(e.message, false); }
}

build();
document.getElementById("settings-save").addEventListener("click", save);
api("GET", "/api/settings").then(fill).catch((e) => say(e.message, false));
