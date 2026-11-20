# ArsipBersih

Local, offline tool to clean up and organize a large personal photo & video collection before importing it into [Immich](https://immich.app).

It finds exact duplicates, re-encoded copies, near-identical burst shots, screenshots, chat-app forwards, blurry or accidental shots, and very short or dark videos. It then sorts the keepers into `Arsip/YYYY/YYYY-MM[ Event]/` and gives you a browser-based review screen where you make every final call. Nothing gets deleted without your decision, and every move can be undone.

> Status: the core engine, the command-line interface and the browser-based review screen (`serve`) are done.

## Requirements
- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- [ExifTool](https://exiftool.org/)
- [ffmpeg / ffprobe](https://ffmpeg.org/)

## Usage

Put your files under `<ROOT>/Sumber/<source-name>/` (one folder per phone or laptop), then:

```
uv run arsipbersih check-tools                 # ExifTool / ffmpeg / ffprobe status
uv run arsipbersih scan ROOT [--workers N]     # resumable; Ctrl+C pauses, run again to resume
uv run arsipbersih events ROOT [--not-event NAME] [--rename OLD=NEW] [--merge FROM=TO]
uv run arsipbersih plan ROOT                   # dry run, moves nothing
uv run arsipbersih tidy ROOT [--yes]           # move into Arsip/ and _review/ (asks first)
uv run arsipbersih report ROOT                 # totals + verify (exit code 1 on mismatch)
uv run arsipbersih undo ROOT --batch ID        # or --all
uv run arsipbersih calibrate ROOT [--sample 200] [--save]
uv run arsipbersih serve ROOT [--port 8765]     # review UI at http://127.0.0.1:8765
```

`tidy` refuses to run if the last `scan` did not finish. Nothing is ever deleted: discarded files go to `_sampah/` and every move is journaled and undoable.

## UI rules (Aturan tampilan)

The review UI (`src/arsipbersih/web/`) is FastAPI + Jinja2 + plain ES modules + plain CSS, with no build step. When you change it:

- **Tokens only.** Colors, font sizes, shadows and radii live in `static/css/tokens.css`. No other CSS file writes a raw color, `font-size` or shadow (tests in `tests/test_web_static.py` enforce this). Small fixed gaps use `--grid-gap`; spacing uses `--space-*`.
- **Components, not page styles.** Shared pieces have one JS module in `static/components/` and one stylesheet in `static/css/components/`: button, segment, toolbar, statusbar, table, list, chip, thumb, compare, group-list, toast, dialog, popover (ⓘ), menu (⋯), steps, danger-zone, sidebar. A page file in `static/css/pages/` may only hold layout (grid, columns, sticky offsets).
- **Folders.** `static/core/` (api, format, keys, storage, size-var), `static/components/`, `static/pages/` (one module per page; it wires components and calls the API), `templates/` (one template per page, macros in `templates/partials/`). Files in `pages/` and `components/` stay at or under 320 lines.
- **Terms.** UI text is Indonesian: Simpan / Disimpan, Buang / Dibuang, Terapkan, Batalkan, Sampah, Review. No English action words (Keep, Delete, Apply, Undo) and no em dash in UI text. Paths such as `_sampah/` appear only where a real path is shown.
- **Accessibility.** Every action works from the keyboard; dialogs, popovers and menus return focus to their trigger; icon buttons carry an `aria-label`; click targets are at least 32 px; text is at least 12 px and meets AA contrast; thumbnail status is never color alone (✓/✕, dimming and `aria-label`). Toasts and the job bar announce through polite live regions.
- **Narrow screens.** No horizontal page scroll at 800 px; the sidebar is closed by default below 1000 px. Every list and grid has an empty, loading and error state using `.empty` (one sentence, at most one action).
- **Motion.** 120 to 200 ms, ease-out; never animate layout properties (width, height, top, left); `prefers-reduced-motion` is respected.
