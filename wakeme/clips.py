"""Evidence snapshots (copied out of the rotating segment ring at fire time) and lazy MP4 cutting."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Optional

Seg = tuple[str, float, float]   # (path, start_pos, end_pos)


def overlapping(segs: list[Seg], t_from: float, t_to: float) -> list[Seg]:
    return [s for s in segs if s[2] > t_from and s[1] < t_to]


def snapshot(segs: list[Seg], dest: Path, t_from: float, t_to: float) -> list[Seg]:
    """Copy the ring segments covering [t_from, t_to] into ``dest`` (merging with earlier copies) so a
    clip asked for minutes later still has the moment after the ring has rotated past it."""
    dest.mkdir(parents=True, exist_ok=True)
    index_path = dest / "index.json"
    have: dict[str, Seg] = {Path(s[0]).name: s for s in load_snapshot(dest)}
    for path, a, b in overlapping(segs, t_from, t_to):
        name = Path(path).name
        if name in have:
            continue
        target = dest / name
        try:
            shutil.copy2(path, target)
        except OSError:
            continue
        have[name] = (str(target), a, b)
    saved = sorted(have.values(), key=lambda s: s[1])
    tmp = index_path.with_name(f".index.{os.getpid()}.{threading.get_ident()}.json")
    tmp.write_text(json.dumps(saved))
    os.replace(tmp, index_path)  # readers never see a half-written index
    return saved


def load_snapshot(dest: Path) -> list[Seg]:
    try:
        return [tuple(s) for s in json.loads((dest / "index.json").read_text())]  # type: ignore[misc]
    except (OSError, ValueError):
        return []


def cut(ffmpeg: str, segs: list[Seg], start: float, duration: float, out: Path) -> Optional[Path]:
    """Concatenate ``segs`` and cut [start, start+duration) (stream positions) into an MP4."""
    segs = sorted(overlapping(segs, start, start + duration), key=lambda s: s[1])
    if not segs:
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    offset = max(0.0, start - segs[0][1])
    length = min(duration, segs[-1][2] - max(start, segs[0][1]))
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, dir=out.parent) as lst:
        for path, _, _ in segs:
            lst.write("file '{}'\n".format(str(path).replace("'", "'\\''")))
        list_path = lst.name
    argv = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-protocol_whitelist", "file", "-f", "concat", "-safe", "0", "-i", list_path,
            "-ss", f"{offset:.2f}", "-t", f"{max(1.0, length):.2f}",
            "-map", "0:v:0?", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(out)]
    try:
        p = subprocess.run(argv, capture_output=True, timeout=120, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return None
    finally:
        Path(list_path).unlink(missing_ok=True)
    return out if p.returncode == 0 and out.exists() and out.stat().st_size > 0 else None
