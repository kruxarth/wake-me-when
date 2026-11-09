"""One ffmpeg per watch (+ streamlink for Twitch): PCM to a reader thread, keyframes, 5 s segments.

The reader thread always drains ffmpeg's stdout into an in-memory ring. If speech recognition falls
behind, the ring drops its oldest audio (and the watch shows ``behind``); the pipe never fills, so
ffmpeg never stalls the live input.
"""

from __future__ import annotations

import collections
import csv
import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from .sources import Source, tool_env

log = logging.getLogger(__name__)

SR = 16000
BYTES_PER_S = SR * 2
KEYFRAME_EVERY = 2.0
SEGMENT_S = 5
RING_SEGMENTS = 40          # ~3 minutes of 5 s segments
AUDIO_RING_S = 180

_LIVE = set()               # every process group we started, for atexit / unload sweeps
_LIVE_LOCK = threading.Lock()


def build_ffmpeg_argv(ffmpeg: str, src: Source, workdir: Path, frames: bool) -> list[str]:
    """argv for the single ffmpeg. Every input carries an explicit ``-protocol_whitelist`` matching the
    source type (network and file protocols are never mixed), and every URL is the value of ``-i``."""
    argv = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"] if src.pipe_argv is None else \
           [ffmpeg, "-hide_banner", "-loglevel", "error"]
    for url in src.inputs:
        if src.paced:
            argv += ["-readrate", f"{src.speed:g}"]
        if src.kind in {"youtube", "hls", "http"} and src.is_live and src.pipe_argv is None:
            argv += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10"]
        if src.kind == "rtsp":
            argv += ["-rtsp_transport", "tcp"]
        argv += ["-protocol_whitelist", src.whitelist, "-i", url]
    audio_in = len(src.inputs) - 1
    argv += ["-map", f"{audio_in}:a:0", "-vn", "-ac", "1", "-ar", str(SR), "-f", "s16le", "pipe:1"]
    if frames:
        argv += ["-map", "0:v:0?", "-an", "-vf", f"fps={1 / KEYFRAME_EVERY:g},scale=640:-2", "-q:v", "6",
                 "-f", "image2", str(workdir / "kf" / "kf_%06d.jpg")]
    argv += ["-map", "0:v:0?", "-map", f"{audio_in}:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "96k",
             "-f", "segment", "-segment_time", str(SEGMENT_S), "-segment_format", "mpegts",
             "-segment_list", str(workdir / "seg" / "list.csv"), "-segment_list_type", "csv",
             str(workdir / "seg" / "s_%06d.ts")]
    return argv


class AudioRing:
    """Thread-safe PCM ring addressed by stream position (seconds since the watch started)."""

    def __init__(self, seconds: float = AUDIO_RING_S):
        self.cap = int(seconds * BYTES_PER_S)
        self.buf = bytearray()
        self.start_pos = 0.0          # stream position of buf[0]
        self.dropped_s = 0.0
        self.lock = threading.Lock()
        self.wall_marks: collections.deque = collections.deque(maxlen=4000)  # (pos, wall)

    @property
    def end_pos(self) -> float:
        return self.start_pos + len(self.buf) / BYTES_PER_S

    def push(self, data: bytes, wall: float) -> None:
        with self.lock:
            self.buf += data
            over = len(self.buf) - self.cap
            if over > 0:
                over += over % 2
                del self.buf[:over]
                self.start_pos += over / BYTES_PER_S
                self.dropped_s += over / BYTES_PER_S
            self.wall_marks.append((self.end_pos, wall))

    def read(self, start: float, end: float) -> tuple[float, bytes]:
        """Bytes for [start, end); start is clamped to what is still buffered."""
        with self.lock:
            s = max(start, self.start_pos)
            a = int((s - self.start_pos) * BYTES_PER_S) & ~1
            b = int((end - self.start_pos) * BYTES_PER_S) & ~1
            return s, bytes(self.buf[a:max(a, b)])

    def wall_at(self, pos: float) -> Optional[float]:
        """Wall-clock time this stream position reached us."""
        with self.lock:
            marks = list(self.wall_marks)
        if not marks:
            return None
        best = min(marks, key=lambda m: abs(m[0] - pos))
        return best[1] - (best[0] - pos)


class Ingest:
    """Owns the ffmpeg (+ streamlink) process group, the reader thread and the segment index."""

    def __init__(self, src: Source, workdir: Path, frames: bool, ring: AudioRing):
        self.src = src
        self.workdir = workdir
        self.frames = frames
        self.ring = ring
        self.proc: Optional[subprocess.Popen] = None
        self.pipe: Optional[subprocess.Popen] = None
        self.pgid: Optional[int] = None
        self.base_pos = 0.0           # stream position where this ffmpeg run started
        self.reader: Optional[threading.Thread] = None
        self.eof = threading.Event()
        self._segments: list[tuple[str, float, float]] = []   # (path, start_pos, end_pos)
        self._csv_seen = 0
        self._seg_base: Optional[float] = None
        self.stderr_tail = ""

    def start(self, base_pos: float = 0.0) -> None:
        from .sources import need
        for sub in ("seg", "kf"):
            (self.workdir / sub).mkdir(parents=True, exist_ok=True)
        (self.workdir / "seg" / "list.csv").unlink(missing_ok=True)
        self._csv_seen, self._seg_base = 0, None
        self.base_pos = base_pos
        self.eof.clear()
        argv = build_ffmpeg_argv(need("ffmpeg"), self.src, self.workdir, self.frames)
        err = open(self.workdir / "ffmpeg.log", "ab")
        env = tool_env()
        if self.src.pipe_argv:
            # A new process group in OUR session: setpgid() into a group of another session is EPERM,
            # so start_new_session here would stop ffmpeg from joining streamlink's group.
            self.pipe = subprocess.Popen(self.src.pipe_argv, stdout=subprocess.PIPE, stderr=err,
                                         stdin=subprocess.DEVNULL, process_group=0, env=env)
            self.pgid = self.pipe.pid
            self.proc = subprocess.Popen(argv, stdin=self.pipe.stdout, stdout=subprocess.PIPE, stderr=err,
                                         process_group=self.pgid, env=env)
            self.pipe.stdout.close()  # ffmpeg owns the read end now
        else:
            self.proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err,
                                         start_new_session=True, env=env)
            self.pgid = self.proc.pid
        err.close()
        with _LIVE_LOCK:
            _LIVE.add(self.pgid)
        self.reader = threading.Thread(target=self._read, name="wakeme-reader", daemon=True)
        self.reader.start()

    def _read(self) -> None:
        proc = self.proc
        pos = self.base_pos
        try:
            while True:
                chunk = proc.stdout.read(BYTES_PER_S // 2)
                if not chunk:
                    break
                self.ring.push(chunk, time.time())
                pos += len(chunk) / BYTES_PER_S
        except (OSError, ValueError):
            pass
        finally:
            self.eof.set()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self) -> None:
        kill_group(self.pgid)
        for p in (self.proc, self.pipe):
            if p is not None:
                try:
                    p.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        with _LIVE_LOCK:
            _LIVE.discard(self.pgid)
        try:
            log_path = self.workdir / "ffmpeg.log"
            self.stderr_tail = log_path.read_text(errors="replace")[-600:] if log_path.exists() else ""
        except OSError:
            self.stderr_tail = ""

    # ---- segments ---------------------------------------------------------------------------

    def segments(self) -> list[tuple[str, float, float]]:
        """Completed segments still on disk as (path, start_pos, end_pos), oldest first."""
        lst = self.workdir / "seg" / "list.csv"
        try:
            with open(lst, newline="") as f:
                rows = list(csv.reader(f))
        except OSError:
            rows = []
        for row in rows[self._csv_seen:]:
            if len(row) < 3:
                continue
            try:
                start, end = float(row[1]), float(row[2])
            except ValueError:
                continue
            if self._seg_base is None:
                self._seg_base = start
            self._segments.append((str(self.workdir / "seg" / row[0]),
                                   self.base_pos + start - self._seg_base, self.base_pos + end - self._seg_base))
        self._csv_seen = len(rows)
        self._segments = [s for s in self._segments if os.path.exists(s[0])]
        return list(self._segments)

    def prune(self, keep: int = RING_SEGMENTS) -> None:
        segs = self.segments()
        for path, _, _ in segs[:-keep] if len(segs) > keep else []:
            try:
                os.unlink(path)
            except OSError:
                pass
        kf = sorted((self.workdir / "kf").glob("kf_*.jpg"))
        for p in kf[:-30]:
            p.unlink(missing_ok=True)

    def latest_keyframe(self) -> Optional[Path]:
        kf = sorted((self.workdir / "kf").glob("kf_*.jpg"))
        return kf[-2] if len(kf) >= 2 else None   # the newest may still be being written


def kill_group(pgid: Optional[int]) -> None:
    if not pgid:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.time() + 3
        while time.time() < deadline:
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                return
            time.sleep(0.1)


def kill_all() -> None:
    """Last-resort sweep (unload / atexit): no orphan ffmpeg or streamlink."""
    with _LIVE_LOCK:
        groups = list(_LIVE)
        _LIVE.clear()
    for g in groups:
        kill_group(g)
