// Zoom bareng. State ternormalisasi {scale, cx, cy}: cx,cy = titik gambar (0..1)
// yang berada di tengah panel. Jendela terlihat selebar 1/scale.
export const MIN_SCALE = 1;
export const MAX_SCALE = 8;

function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }

function clampCenter(state) {
  const half = 0.5 / state.scale;
  state.cx = clamp(state.cx, half, 1 - half);
  state.cy = clamp(state.cy, half, 1 - half);
  return state;
}

// px,py: posisi kursor di panel (0..1). Titik gambar di bawah kursor tetap di tempat.
export function zoomAt(state, factor, px, py) {
  const old = state.scale;
  const next = clamp(old * factor, MIN_SCALE, MAX_SCALE);
  const ux = state.cx + (px - 0.5) / old;
  const uy = state.cy + (py - 0.5) / old;
  state.scale = next;
  state.cx = ux - (px - 0.5) / next;
  state.cy = uy - (py - 0.5) / next;
  return clampCenter(state);
}

// dx,dy: geseran isi gambar sebagai fraksi lebar/tinggi panel (drag ke kanan = positif).
export function panBy(state, dx, dy) {
  state.cx -= dx / state.scale;
  state.cy -= dy / state.scale;
  return clampCenter(state);
}

export function createZoom(panes) {
  const state = { scale: 1, cx: 0.5, cy: 0.5 };
  const ac = new AbortController();
  const opts = { signal: ac.signal };

  function imgOf(pane) { return pane.querySelector("img") || pane; }

  function apply() {
    const tx = (0.5 - state.cx * state.scale) * 100;
    const ty = (0.5 - state.cy * state.scale) * 100;
    for (const pane of panes) {
      const img = imgOf(pane);
      img.style.transformOrigin = "0 0";
      img.style.transform = `translate(${tx}%, ${ty}%) scale(${state.scale})`;
      pane.classList.toggle("zoomed", state.scale > 1);
    }
  }

  function frac(pane, ev) {
    const r = pane.getBoundingClientRect();
    return [(ev.clientX - r.left) / (r.width || 1), (ev.clientY - r.top) / (r.height || 1)];
  }

  function reset() {
    state.scale = 1; state.cx = 0.5; state.cy = 0.5;
    apply();
  }

  for (const pane of panes) {
    pane.addEventListener("wheel", (ev) => {
      ev.preventDefault();
      const [px, py] = frac(pane, ev);
      zoomAt(state, ev.deltaY < 0 ? 1.2 : 1 / 1.2, px, py);
      apply();
    }, { passive: false, signal: ac.signal });

    pane.addEventListener("pointerdown", (ev) => {
      if (ev.button !== 0 || state.scale <= 1) return;
      ev.preventDefault();
      let last = [ev.clientX, ev.clientY];
      const r = pane.getBoundingClientRect();
      pane.setPointerCapture(ev.pointerId);
      pane.classList.add("dragging");
      const move = (e) => {
        panBy(state, (e.clientX - last[0]) / (r.width || 1), (e.clientY - last[1]) / (r.height || 1));
        last = [e.clientX, e.clientY];
        apply();
      };
      const up = () => {
        pane.classList.remove("dragging");
        dragAc.abort();
      };
      const dragAc = new AbortController();
      const dopts = { signal: AbortSignal.any([ac.signal, dragAc.signal]) };
      pane.addEventListener("pointermove", move, dopts);
      pane.addEventListener("pointerup", up, dopts);
      pane.addEventListener("pointercancel", up, dopts);
    }, opts);

    pane.addEventListener("dblclick", reset, opts);
  }

  apply();
  return {
    reset,
    destroy() {
      ac.abort();
      for (const pane of panes) {
        imgOf(pane).style.transform = "";
        pane.classList.remove("zoomed", "dragging");
      }
    },
  };
}
