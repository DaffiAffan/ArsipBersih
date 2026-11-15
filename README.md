# ArsipBersih

Local, offline tool to clean up and organize a large personal photo & video collection before importing it into [Immich](https://immich.app).

It finds exact duplicates, re-encoded copies, near-identical burst shots, screenshots, chat-app forwards, blurry or accidental shots, and very short or dark videos. It then sorts the keepers into `Arsip/YYYY/YYYY-MM[ Event]/` and gives you a browser-based review screen where you make every final call. Nothing gets deleted without your decision, and every move can be undone.

> Status: design phase. No code yet.

## Requirements (planned)
- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- [ExifTool](https://exiftool.org/)
- [ffmpeg / ffprobe](https://ffmpeg.org/)
