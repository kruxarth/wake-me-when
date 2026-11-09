"""`/wakeme doctor` and `hermes wakeme doctor`: dependency checks, a 20-second demo watch, pass/fail."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional

from . import asr, cues, sources
from .ingest import AudioRing, Ingest

OK, WARN, FAIL = "✓", "!", "✗"


def _version(argv: list[str]) -> str:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL,
                           env=sources.tool_env())
        return (p.stdout or p.stderr).strip().splitlines()[0][:60]
    except (OSError, subprocess.TimeoutExpired, IndexError):
        return ""


def check_tools() -> list[tuple[str, str, str]]:
    rows = []
    for name, why, required in (("ffmpeg", "ingest and clips", True), ("ffprobe", "media probing", True),
                                ("yt-dlp", "YouTube", False), ("streamlink", "Twitch", False),
                                ("tesseract", "on-screen text", False)):
        path = sources.which(name)
        if path:
            flag = "-version" if name in {"ffmpeg", "ffprobe"} else "--version"
            rows.append((OK, name, f"{_version([path, flag])} ({why})"))
        else:
            rows.append((FAIL if required else WARN, name, f"missing — {sources.INSTALL_HINTS[name]}"))
    if sources.which("yt-dlp"):
        rt = next((r for r in ("deno", "node", "bun") if sources.which(r)), None)
        rows.append((OK, "JS runtime", f"{rt} (yt-dlp YouTube extraction)") if rt else
                    (WARN, "JS runtime", sources.INSTALL_HINTS["deno"]))
    return rows


def check_asr(model: str, install: bool) -> tuple[str, str, str]:
    if not install and not asr.faster_whisper_importable():
        return WARN, "speech recognition", "faster-whisper not installed yet (run /wakeme doctor to install)"
    t = time.time()
    ok, msg = asr.load_model(model)
    if not ok:
        return WARN, "speech recognition", f"{msg}. Demo watches use their captions meanwhile."
    pcm = _demo_pcm(seconds=12, offset=50)
    text = " ".join(s.text for s in asr.transcribe(pcm, 0.0, "en")) if pcm else ""
    took = time.time() - t
    heard = f' — heard: “{text[:70]}”' if text else ""
    return OK, "speech recognition", f"faster-whisper {model} ready in {took:.1f}s{heard}"


def _demo_pcm(seconds: float, offset: float) -> bytes:
    ffmpeg = sources.which("ffmpeg")
    if not ffmpeg:
        return b""
    p = subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-protocol_whitelist", "file", "-ss", str(offset),
                        "-t", str(seconds), "-i", str(sources.DEMO_DIR / "keynote.mp4"), "-ac", "1", "-ar", "16000",
                        "-f", "s16le", "pipe:1"], capture_output=True, timeout=30, stdin=subprocess.DEVNULL,
                       env=sources.tool_env())
    return p.stdout


def demo_watch(seconds: float = 20.0) -> tuple[str, str, str]:
    """Real ingest of demo://keynote?speed=4 for ~20 s: segments, audio, and a cue hit on the price."""
    if not sources.which("ffmpeg"):
        return FAIL, "demo watch", "needs ffmpeg"
    src = sources.resolve("demo://keynote?speed=4", "demo")
    work = Path(tempfile.mkdtemp(prefix="wakeme-doctor-"))
    ring = AudioRing()
    ing = Ingest(src, work, frames=False, ring=ring)
    t = time.time()
    try:
        ing.start()
        while time.time() - t < seconds and not ing.eof.is_set():
            time.sleep(0.5)
        audio_s = ring.end_pos
        segs = ing.segments()
    finally:
        ing.stop()
        shutil.rmtree(work, ignore_errors=True)
    rows = [(i, c.text) for i, c in enumerate(src.captions or []) if c.start < audio_s]
    hit = cues.match(rows, ["dollars", "seven hundred and ninety nine", "price"])
    if audio_s < 30:
        return FAIL, "demo watch", f"only {audio_s:.0f}s of audio in {seconds:.0f}s (expected ~70s at 4x)"
    detail = f"{audio_s:.0f}s of audio and {len(segs)} clip segments in {time.time() - t:.0f}s"
    if not hit.cues:
        return WARN, "demo watch", detail + "; price cue not reached"
    return OK, "demo watch", detail + f"; cue “{hit.cues[0]}” matched"


def check_model(judge: Optional[Callable[[], float]]) -> tuple[str, str, str]:
    if judge is None:
        return WARN, "model", "not checked (no ctx.llm in this process)"
    try:
        took = judge()
        return OK, "model", f"judge call answered in {took:.1f}s"
    except Exception as exc:
        return WARN, "model", f"judge call failed: {str(exc)[:120]} (watches fall back to cue matching)"


def run(model: str = "base", install: bool = True, judge: Optional[Callable[[], float]] = None,
        demo_seconds: float = 20.0) -> str:
    rows = check_tools()
    rows.append(check_asr(model, install))
    try:
        rows.append(demo_watch(demo_seconds))
    except Exception as exc:
        rows.append((FAIL, "demo watch", str(exc)[:160]))
    rows.append(check_model(judge))
    fails = sum(1 for r in rows if r[0] == FAIL)
    warns = sum(1 for r in rows if r[0] == WARN)
    lines = ["🩺 Wake Me When doctor"] + [f"{a} {b}: {c}" for a, b, c in rows]
    verdict = "FAIL" if fails else "ready"
    lines.append(f"Result: {verdict} ({fails} failed, {warns} warning{'s' if warns != 1 else ''})")
    if not fails:
        lines.append("Try: /wakeme demo://keynote when they announce the price")
    return "\n".join(lines)


def judge_probe(app: Any) -> Optional[Callable[[], float]]:
    if getattr(app.ctx, "llm", None) is None:
        return None
    from .judge import JUDGE_INSTRUCTIONS, JUDGE_SCHEMA, build_judge_input

    def probe() -> float:
        t = time.time()
        text = build_judge_input("they announce the price",
                                 [(1, "So, how much is it? It starts at just seven hundred and ninety nine dollars.")])
        parsed = app.mgr.llm.structured(JUDGE_INSTRUCTIONS, text, JUDGE_SCHEMA, task="wakeme_judge",
                                        purpose="wakeme.doctor", max_tokens=300, timeout=90)
        if not isinstance(parsed, dict):
            raise RuntimeError("no JSON verdict")
        return time.time() - t
    return probe
