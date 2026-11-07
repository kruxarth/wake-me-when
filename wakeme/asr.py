"""ONE shared faster-whisper model and ONE worker thread serving every watch, round-robin.

faster-whisper comes from Hermes's own ``stt-whisper`` extra, installed on demand through the
public ``pm.ensure_import`` helper (``doctor`` pre-warms it). Until it is ready, demo watches and
files with sidecar captions use their captions instead.
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger(__name__)

WINDOW_S = 10.0
OVERLAP_S = 2.0
MAX_QUEUE = 6               # windows per watch (~48 s) before we start dropping


@dataclass
class Segment:
    start: float            # stream position (s)
    end: float
    text: str


@dataclass
class Job:
    watch_id: int
    pos: float              # stream position of the window start
    pcm: bytes
    first: bool
    final: bool
    done: Callable[[list[Segment]], None]
    loud: bool = True       # cheap energy check; quiet windows are dropped first when behind


class _State:
    def __init__(self) -> None:
        self.status = "missing"     # missing | installing | loading | ready | failed
        self.detail = ""
        self.model = None
        self.model_name = ""
        self.lock = threading.Lock()


STATE = _State()


def faster_whisper_importable() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:
        return False


def ensure_installed() -> tuple[bool, str]:
    """Install Hermes's ``stt-whisper`` extra if needed. Returns (ok, message)."""
    if faster_whisper_importable():
        return True, "faster-whisper is installed"
    try:
        from pm import InstallError, ensure_import
    except ImportError:
        return False, "faster-whisper is not installed (run `hermes wakeme doctor` inside Hermes)"
    try:
        ensure_import("stt-whisper")
    except InstallError as exc:
        return False, f"could not install speech recognition: {exc}"
    except Exception as exc:  # pm boundary: never let an installer error escape a watch thread
        return False, f"could not install speech recognition: {exc}"
    if faster_whisper_importable():
        return True, "installed faster-whisper (Hermes stt-whisper extra)"
    return False, "installed speech recognition; restart Hermes (`hermes gateway restart`) to load it"


def load_model(name: str = "base") -> tuple[bool, str]:
    """Install (if needed) and load the shared model once. Safe to call from many threads."""
    with STATE.lock:
        if STATE.status == "ready" and STATE.model_name == name:
            return True, "ready"
        STATE.status, STATE.detail = "installing", ""
    ok, msg = ensure_installed()
    if not ok:
        with STATE.lock:
            STATE.status, STATE.detail = "failed", msg
        return False, msg
    with STATE.lock:
        STATE.status = "loading"
    try:
        from faster_whisper import WhisperModel
        t = time.time()
        model = WhisperModel(name, device="cpu", compute_type="int8", cpu_threads=4)
        log.info("wake-me-when: loaded whisper %s in %.1fs", name, time.time() - t)
    except Exception as exc:
        with STATE.lock:
            STATE.status, STATE.detail = "failed", f"could not load the {name} speech model: {exc}"[:300]
        return False, STATE.detail
    with STATE.lock:
        STATE.model, STATE.model_name, STATE.status, STATE.detail = model, name, "ready", ""
    return True, "ready"


def ready() -> bool:
    return STATE.status == "ready" and STATE.model is not None


def _rms(pcm: bytes) -> float:
    import array
    a = array.array("h", pcm[: len(pcm) - len(pcm) % 2])
    if not a:
        return 0.0
    step = max(1, len(a) // 4000)
    sample = a[::step]
    return (sum(x * x for x in sample) / len(sample)) ** 0.5


def keep_segment(seg_start: float, seg_end: float, win_pos: float, first: bool, final: bool) -> bool:
    """Overlapping windows: each spoken segment is kept by exactly one window (split at mid-overlap)."""
    mid = (seg_start + seg_end) / 2
    lo = win_pos if first else win_pos + OVERLAP_S / 2
    hi = float("inf") if final else win_pos + WINDOW_S - OVERLAP_S / 2
    return lo <= mid < hi


class Worker:
    """Single transcription thread. ``submit`` never blocks; queues are bounded per watch."""

    def __init__(self) -> None:
        self.queues: dict[int, collections.deque] = {}
        self.behind: dict[int, bool] = {}
        self.cond = threading.Condition()
        self.thread: Optional[threading.Thread] = None
        self.stopping = False
        self.language: Optional[str] = None
        self.busy_s = 0.0

    def start(self) -> None:
        with self.cond:
            if self.thread and self.thread.is_alive():
                return
            self.stopping = False
            self.thread = threading.Thread(target=self._loop, name="wakeme-asr", daemon=True)
            self.thread.start()

    def stop(self) -> None:
        with self.cond:
            self.stopping = True
            self.cond.notify_all()

    def submit(self, job: Job) -> None:
        job.loud = _rms(job.pcm) > 120
        with self.cond:
            q = self.queues.setdefault(job.watch_id, collections.deque())
            q.append(job)
            while len(q) > MAX_QUEUE:
                quiet = next((j for j in q if not j.loud and not j.final), None)
                q.remove(quiet if quiet is not None else q[0])
                self.behind[job.watch_id] = True
            self.cond.notify()

    def drop(self, watch_id: int) -> None:
        with self.cond:
            self.queues.pop(watch_id, None)
            self.behind.pop(watch_id, None)

    def backlog(self, watch_id: int) -> int:
        with self.cond:
            return len(self.queues.get(watch_id, ()))

    def _next(self) -> Optional[Job]:
        with self.cond:
            while not self.stopping:
                if ready():
                    for wid in sorted(self.queues, key=lambda w: self.queues[w][0].pos if self.queues[w] else 1e18):
                        q = self.queues[wid]
                        if q:
                            job = q.popleft()
                            if not q:
                                self.behind[wid] = False
                            # rotate: next round starts with a different watch
                            self.queues[wid] = self.queues.pop(wid)
                            return job
                self.cond.wait(timeout=1.0)
            return None

    def _loop(self) -> None:
        while True:
            job = self._next()
            if job is None:
                return
            t = time.time()
            segs: list[Segment] = []
            if job.loud or job.final:
                try:
                    segs = transcribe(job.pcm, job.pos, self.language)
                except Exception:
                    log.warning("wake-me-when: transcription failed", exc_info=True)
            self.busy_s += time.time() - t
            kept = [s for s in segs if keep_segment(s.start, s.end, job.pos, job.first, job.final)]
            try:
                job.done(kept)
            except Exception:
                log.warning("wake-me-when: transcript callback failed", exc_info=True)


def transcribe(pcm: bytes, pos: float, language: Optional[str] = None) -> list[Segment]:
    import numpy as np
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    segs, _ = STATE.model.transcribe(audio, language=language or None, vad_filter=True, beam_size=1,
                                     condition_on_previous_text=False)
    return [Segment(pos + s.start, pos + s.end, s.text.strip()) for s in segs if s.text.strip()]


def transcribe_file_pcm(pcm: bytes, language: Optional[str] = None, chunk_s: float = 30.0) -> list[Segment]:
    """Whole-recording transcription for find mode (sequential 30 s chunks, no overlap needed)."""
    out: list[Segment] = []
    step = int(chunk_s * 32000)
    for i in range(0, len(pcm), step):
        out += transcribe(pcm[i:i + step], i / 32000, language)
    return out


def captions_for(cues: list, win_pos: float, first: bool, final: bool) -> list[Segment]:
    """Caption fallback: cues of the source (stream-time) that this window owns."""
    lo = win_pos if first else win_pos + OVERLAP_S / 2
    hi = float("inf") if final else win_pos + WINDOW_S - OVERLAP_S / 2
    return [Segment(c.start, c.end, c.text) for c in cues if lo <= c.start < hi]


WORKER = Worker()
