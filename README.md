# ArsipBersih

Local, offline tool to clean up and organize a large personal photo & video collection before importing it into [Immich](https://immich.app).

It finds exact duplicates, re-encoded copies, near-identical burst shots, screenshots, chat-app forwards, blurry or accidental shots, and very short or dark videos. It then sorts the keepers into `Arsip/YYYY/YYYY-MM[ Event]/` and gives you a browser-based review screen where you make every final call. Nothing gets deleted without your decision, and every move can be undone.

> Status: design phase. No code yet.

## Requirements (planned)
- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- [ExifTool](https://exiftool.org/)
- [ffmpeg / ffprobe](https://ffmpeg.org/)

## Usage

Put your files under `<ROOT>/Sumber/<source-name>/` (one folder per phone or laptop), then:

```
uv run arsipbersih check-tools                 # ExifTool / ffmpeg / ffprobe status
uv run arsipbersih scan ROOT [--workers N]     # resumable; Ctrl+C pauses, run again to resume
uv run arsipbersih events ROOT [--not-event NAME] [--rename OLD NEW] [--merge FROM TO]
uv run arsipbersih plan ROOT                   # dry run, moves nothing
uv run arsipbersih tidy ROOT [--yes]           # move into Arsip/ and _review/ (asks first)
uv run arsipbersih report ROOT                 # totals + verify (exit code 1 on mismatch)
uv run arsipbersih undo ROOT --batch ID        # or --all
uv run arsipbersih calibrate ROOT [--sample 200] [--save]
```

`tidy` refuses to run if the last `scan` did not finish. Nothing is ever deleted: discarded files go to `_sampah/` and every move is journaled and undoable.
