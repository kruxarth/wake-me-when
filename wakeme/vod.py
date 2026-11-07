"""Find mode: search a recording for a moment and return ranked timestamps with deep links.

Captions first (yt-dlp subtitles, demo VTT, sidecar .vtt/.srt), local speech recognition as the
fallback. Ranking: cheap keyword scoring to shortlist passages, then one LLM rerank.
"""

from __future__ import annotations

import glob
import json
import math
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import asr, cues, plan as planmod, sources, vtt

MAX_ASR_SECONDS = 3 * 3600
PASSAGE_S = 30.0
HOP_S = 15.0

RERANK_SCHEMA = {
    "type": "object",
    "properties": {"results": {"type": "array", "items": {
        "type": "object",
        "properties": {"passage_id": {"type": "integer"}, "score": {"type": "number"},
                       "evidence": {"type": "string"}},
        "required": ["passage_id", "score", "evidence"]}}},
    "required": ["results"],
}

RERANK_INSTRUCTIONS = """WAKEME_FIND
You search a recording's transcript for the moments that answer the user's question.
You get numbered passages "[id] (m:ss) text". Return up to 5 passages that best answer it, best
first, each with score 0..1 and evidence: the most relevant exact sentence (under 25 words).
Only include passages that really match; return an empty list if none do. JSON only."""


@dataclass
class Passage:
    pid: int
    start: float
    text: str
    cues: list = None  # type: ignore[assignment]  # [(start, text)] inside the passage


@dataclass
class Hit:
    start: float
    score: float
    evidence: str


class FindError(Exception):
    pass


def passages_from(segments: list[tuple[float, str]]) -> list[Passage]:
    out: list[Passage] = []
    if not segments:
        return out
    t, end = 0.0, segments[-1][0] + 1
    pid = 0
    while t < end:
        inside = [(p, s) for p, s in segments if t <= p < t + PASSAGE_S]
        text = " ".join(s for _, s in inside)
        if text.strip():
            out.append(Passage(pid, inside[0][0], text, inside))
            pid += 1
        t += HOP_S
    return out


def keyword_rank(passages: list[Passage], question: str, extra_terms: list[str] = ()) -> list[tuple[float, Passage]]:
    terms = [cues.normalize(t) for t in (planmod.heuristic_cues(question) + list(extra_terms)) if cues.normalize(t)]
    if not terms:
        return [(0.0, p) for p in passages]
    docs = [cues.normalize(p.text) for p in passages]
    n = len(docs)
    scored = []
    for p, d in zip(passages, docs):
        score = 0.0
        for term in terms:
            df = sum(1 for x in docs if cues.phrase_in(term, x)) or 1
            if cues.phrase_in(term, d):
                score += math.log(1 + n / df)
        scored.append((score, p))
    scored.sort(key=lambda x: -x[0])
    return scored


def best_sentence(text: str, question: str) -> str:
    sents = re.split(r"(?<=[.!?])\s+", text)
    terms = planmod.heuristic_cues(question)
    best = max(sents, key=lambda s: sum(cues.phrase_in(cues.normalize(t), cues.normalize(s)) for t in terms))
    return best.strip()[:200]


def _youtube_captions(url: str, workdir: Path) -> tuple[dict, list[vtt.Cue]]:
    info = sources.ytdlp_info(url, fmt=None)
    if info.get("is_live"):
        raise FindError("That's a live stream. Use /wakeme <url> when <condition> to watch it.")
    argv = [sources.need("yt-dlp"), "--skip-download", "--no-warnings", "--no-playlist", *sources.js_runtime_args(),
            "--write-subs", "--write-auto-subs", "--sub-langs", "en.*,en,-live_chat", "--sub-format", "vtt",
            "-o", str(workdir / "%(id)s.%(ext)s"), "--", url]
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=90, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return info, []
    files = sorted(glob.glob(str(workdir / "*.vtt")), key=lambda f: ("orig" in f, len(f)))
    if not files:
        if p.returncode != 0:
            info["_caption_error"] = sources._short_err(p.stderr)
        return info, []
    return info, vtt.parse(Path(files[0]).read_text(encoding="utf-8", errors="replace"))


def _asr_media(target: str, whitelist: str, language: Optional[str], pre: list[str] = ()) -> list[vtt.Cue]:
    if not asr.ready():
        ok, msg = asr.load_model()
        if not ok:
            raise FindError(f"No captions, and local speech recognition isn't available: {msg}")
    argv = [sources.need("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", *pre,
            "-protocol_whitelist", whitelist, "-i", target, "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le",
            "-t", str(MAX_ASR_SECONDS), "pipe:1"]
    try:
        p = subprocess.run(argv, capture_output=True, timeout=900, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise FindError("Reading the audio took too long.") from exc
    if p.returncode != 0 or not p.stdout:
        raise FindError("Couldn't read audio from that source.")
    return [vtt.Cue(s.start, s.end, s.text) for s in asr.transcribe_file_pcm(p.stdout, language)]


def _youtube_audio_asr(url: str, info: dict, workdir: Path, language: Optional[str]) -> list[vtt.Cue]:
    if (info.get("duration") or 0) > MAX_ASR_SECONDS:
        raise FindError("No captions, and the video is too long to transcribe locally (3 h max).")
    fmts = sources.ytdlp_info(url, fmt="ba/b").get("requested_formats") or []
    audio_url = (fmts[0] if fmts else {}).get("url") or sources.ytdlp_info(url, fmt="ba/b").get("url")
    if not audio_url:
        raise FindError("YouTube gave no audio stream.")
    return _asr_media(audio_url, sources.NET_WHITELIST, language)


def transcript_for(url: str, kind: str, language: Optional[str]) -> tuple[sources.Source, list[vtt.Cue], str]:
    """Returns (source, cues, how) where how is 'captions' / 'speech recognition'."""
    with tempfile.TemporaryDirectory(prefix="wakeme-find-") as td:
        work = Path(td)
        if kind == "youtube":
            info, caps = _youtube_captions(url, work)
            src = sources.Source(url=url, kind="youtube", title=info.get("title") or url, is_live=False,
                                 video_id=info.get("id") or "", duration=info.get("duration"),
                                 channel=info.get("channel") or "")
            if caps:
                return src, caps, "captions"
            why = info.get("_caption_error")
            try:
                return src, _youtube_audio_asr(url, info, work, language), (
                    f"speech recognition (YouTube refused captions: {why})" if why else "speech recognition (no captions)")
            except sources.SourceError as exc:
                raise FindError(f"YouTube refused: {exc}") from exc
        if kind == "twitch":
            raise FindError("Search works on YouTube videos, web media links, local files and demo://. "
                            "For Twitch, watch it live instead.")
        src = sources.resolve(url, kind)
        if src.is_live and kind not in {"demo"}:
            raise FindError("That's a live stream. Use /wakeme <url> when <condition> to watch it.")
        if kind == "demo":
            if asr.ready():
                return src, _asr_media(src.inputs[0], sources.FILE_WHITELIST, language), "speech recognition"
            return src, src.captions or [], "captions"
        if src.captions:
            return src, src.captions, "captions"
        if src.duration and src.duration > MAX_ASR_SECONDS:
            raise FindError("That recording is longer than 3 hours; add a .vtt/.srt next to it to search it.")
        return src, _asr_media(src.inputs[0], src.whitelist, language), "speech recognition"


def rank(passages: list[Passage], question: str,
         rerank: Optional[Callable[[str, str, dict], dict]]) -> tuple[list[Hit], bool]:
    """Returns (hits, used_model)."""
    shortlist = [p for s, p in keyword_rank(passages, question)[:24]]
    if len(passages) <= 24:
        shortlist = passages
    if rerank is not None and shortlist:
        body = "\n".join(f"[{p.pid}] ({sources.fmt_pos(p.start)}) {p.text[:700]}" for p in shortlist)
        try:
            parsed = rerank(RERANK_INSTRUCTIONS, f"Question: {question}\nPassages:\n{body}", RERANK_SCHEMA)
            by_id = {p.pid: p for p in shortlist}
            hits = []
            for r in (parsed or {}).get("results", []):
                p = by_id.get(int(r.get("passage_id", -1)))
                if p is None or float(r.get("score") or 0) < 0.35:
                    continue
                ev = str(r.get("evidence") or "").strip() or best_sentence(p.text, question)
                hits.append(Hit(_refine_start(p, ev), float(r.get("score") or 0), ev[:200]))
            return _dedupe(hits)[:5], True
        except Exception:
            pass
    scored = [(s, p) for s, p in keyword_rank(passages, question) if s > 0][:5]
    top = scored[0][0] if scored else 1.0
    hits = []
    for s, p in scored:
        ev = best_sentence(p.text, question)
        hits.append(Hit(_refine_start(p, ev), s / top, ev))
    return _dedupe(hits), False


def _refine_start(p: Passage, evidence: str) -> float:
    """Point the timestamp at the cue that contains the evidence, not the passage start."""
    ev = cues.normalize(evidence)[:60]
    for t, text in p.cues or []:
        nt = cues.normalize(text)
        if ev and (ev[:30] in nt or nt[:30] in ev):
            return t
    return p.start


def _dedupe(hits: list[Hit]) -> list[Hit]:
    out: list[Hit] = []
    for h in hits:
        if all(abs(h.start - o.start) > 20 for o in out):
            out.append(h)
    return out


def find(url: str, question: str, *, surface: str, allow_private: bool, allowed_dirs: list[str],
         language: Optional[str], rerank: Optional[Callable]) -> str:
    try:
        kind = sources.check_url(url, surface=surface, allow_private=allow_private, allowed_dirs=allowed_dirs)
        src, caps, how = transcript_for(url, kind, language)
    except (sources.SourceError, FindError) as exc:
        return f"🔎 Couldn't search that: {exc}"
    segments = [(c.start, c.text) for c in caps]
    if not segments:
        return f"🔎 {src.title}: no speech found to search."
    hits, used_model = rank(passages_from(segments), question, rerank)
    head = f"🔎 “{question}” in {src.title} ({how}{'' if used_model else ', keyword match only'})"
    if not hits:
        return f"{head}\nNo matching moment found."
    lines = [head]
    for i, h in enumerate(hits, 1):
        lines.append(f"{i}. {sources.fmt_pos(h.start)} — “{h.evidence}”\n   {src.link(h.start) if src.kind != 'demo' else src.url + f' (at {sources.fmt_pos(h.start)})'}")
    return "\n".join(lines)


def to_json(text: str) -> str:
    return json.dumps({"result": text})
