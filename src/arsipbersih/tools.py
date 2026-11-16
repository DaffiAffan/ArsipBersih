"""Locate and version-check external tools (ExifTool, ffmpeg, ffprobe)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Child tools (ExifTool, ffprobe, ffmpeg) run without a console and in their own process
# group, so a console Ctrl+C reaches only the scan, which pauses through its stop event,
# instead of killing a tool mid-batch.
NO_CONSOLE_FLAGS = (
    subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    if sys.platform == "win32"
    else 0
)


def find_tool(name: str) -> str | None:
    """Absolute path of a tool: PATH, then <repo>/tools/**, then the winget ExifTool location."""
    found = shutil.which(name)
    if found:
        return str(Path(found).resolve())
    tools_dir = _REPO_ROOT / "tools"
    if tools_dir.is_dir():
        for candidate in (f"{name}.exe", name):
            for hit in tools_dir.rglob(candidate):
                if hit.is_file():
                    return str(hit.resolve())
    if name.casefold() == "exiftool":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            p = Path(local) / "Programs" / "ExifTool" / "ExifTool.exe"
            if p.is_file():
                return str(p)
    return None


@dataclass
class ToolStatus:
    exiftool: str | None
    ffmpeg: str | None
    ffprobe: str | None


def _first_line(path: str, arg: str) -> str | None:
    try:
        out = subprocess.run(
            [path, arg], capture_output=True, text=True, timeout=15, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    lines = out.strip().splitlines()
    return lines[0].strip() if lines else None


def check_tools() -> ToolStatus:
    """Version strings for each tool, or None when missing."""
    et = find_tool("exiftool")
    ff = find_tool("ffmpeg")
    fp = find_tool("ffprobe")
    return ToolStatus(
        exiftool=_first_line(et, "-ver") if et else None,
        ffmpeg=_first_line(ff, "-version") if ff else None,
        ffprobe=_first_line(fp, "-version") if fp else None,
    )
