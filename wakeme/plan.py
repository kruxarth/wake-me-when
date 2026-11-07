"""Condition -> watch plan (one structured LLM call per watch), plus command-line parsing."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "cue_phrases": {"type": "array", "items": {"type": "string"}},
        "ocr_patterns": {"type": "array", "items": {"type": "string"}},
        "visual_question": {"type": ["string", "null"]},
        "negative_cues": {"type": "array", "items": {"type": "string"}},
        "confirm": {"type": "string", "enum": ["once", "twice"]},
        "min_confidence": {"type": "number"},
    },
    "required": ["summary", "cue_phrases", "ocr_patterns", "visual_question", "negative_cues", "confirm",
                 "min_confidence"],
}

PLAN_INSTRUCTIONS = """WAKEME_PLAN
Turn a user's "wake me when ..." condition for a live audio/video stream into a watch plan.
- summary: the condition restated in under 12 words.
- cue_phrases: 4-12 short words/phrases a speaker would SAY when it happens, lower case. Include
  name variants, numbers written both ways ("799", "seven ninety nine") and likely speech-recognition
  misspellings of names.
- ocr_patterns: 0-4 Python regexes for ON-SCREEN text that would show it (scoreboard, lower third,
  price slide); [] if nothing would be written on screen.
- visual_question: a yes/no question about a video frame ONLY if the moment is purely visual (an
  animal appears, a rocket lifts off on camera); otherwise null.
- negative_cues: phrases that sound related but mean it has NOT happened ("in a moment", "later").
- confirm: "twice" when a single mention is easily ambiguous, else "once".
- min_confidence: 0.5-0.8.
Reply with JSON only."""


@dataclass
class Plan:
    summary: str
    cue_phrases: list[str] = field(default_factory=list)
    ocr_patterns: list[str] = field(default_factory=list)
    visual_question: Optional[str] = None
    negative_cues: list[str] = field(default_factory=list)
    confirm: str = "once"
    min_confidence: float = 0.6

    @property
    def needs_frames(self) -> bool:
        return bool(self.ocr_patterns or self.visual_question)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict, condition: str = "") -> "Plan":
        def strs(key: str, limit: int) -> list[str]:
            vals = d.get(key) or []
            if isinstance(vals, str):
                vals = [vals]
            out = []
            for v in vals:
                v = str(v).strip()
                if v and v.lower() not in {o.lower() for o in out}:
                    out.append(v[:80])
            return out[:limit]

        ocr = []
        for p in strs("ocr_patterns", 4):
            try:
                re.compile(p)
                ocr.append(p)
            except re.error:
                ocr.append(re.escape(p))
        vq = d.get("visual_question")
        vq = str(vq).strip()[:200] if vq and str(vq).strip().lower() not in {"null", "none"} else None
        try:
            mc = float(d.get("min_confidence", 0.6))
        except (TypeError, ValueError):
            mc = 0.6
        return cls(
            summary=str(d.get("summary") or condition).strip()[:120] or condition,
            cue_phrases=[c.lower() for c in strs("cue_phrases", 16)],
            ocr_patterns=ocr,
            visual_question=vq,
            negative_cues=[c.lower() for c in strs("negative_cues", 8)],
            confirm="twice" if str(d.get("confirm")).lower() == "twice" else "once",
            min_confidence=min(max(mc, 0.4), 0.9),
        )


def make_plan(llm_call, condition: str) -> Plan:
    """``llm_call(instructions, text, schema) -> parsed dict or None`` (injected so tests can stub it).
    Raises RuntimeError when the model returns nothing usable."""
    parsed = llm_call(PLAN_INSTRUCTIONS, f"Condition: {condition}", PLAN_SCHEMA)
    if not isinstance(parsed, dict) or not parsed.get("cue_phrases"):
        raise RuntimeError("the model did not return a usable plan")
    return Plan.from_dict(parsed, condition)


# ---- command parsing ------------------------------------------------------------------------

_URLISH = re.compile(r"^(?:https?://|rtsp://|demo://|file://|/|~/|\./)\S*$|^\S+\.(?:m3u8|mp4|mkv|webm|mp3|wav|m4a|ts|mov)$", re.I)


@dataclass
class WatchRequest:
    url: str
    condition: str
    mode: str = "once"  # once | every
    cooldown_min: float = 5.0


def parse_watch(raw: str) -> WatchRequest:
    """Parse ``<url> when <condition>`` (or ``when <condition> <url>`` / ``<condition> <url>``) with an
    optional ``--every [minutes]`` flag. Raises ValueError with a usage hint."""
    text = raw.strip()
    mode, cooldown = "once", 5.0
    m = re.search(r"\s--every(?:[ =](\d+(?:\.\d+)?))?\b", " " + text)
    if m:
        mode = "every"
        if m.group(1):
            cooldown = float(m.group(1))
        text = (" " + text).replace(m.group(0), " ").strip()
    tokens = text.split()
    url_idx = next((i for i, t in enumerate(tokens) if _URLISH.match(t)), None)
    if url_idx is None:
        raise ValueError("I need a stream link. Usage: /wakeme <url> when <condition>")
    url = tokens.pop(url_idx)
    cond = " ".join(tokens).strip()
    cond = re.sub(r"^(?:when|if|once|as soon as)\s+", "", cond, flags=re.I).strip(" ,.")
    if len(cond) < 3:
        raise ValueError("What should I wait for? Usage: /wakeme <url> when <condition>")
    return WatchRequest(url=url.strip("<>"), condition=cond[:300], mode=mode, cooldown_min=max(1.0, cooldown))


def heuristic_cues(condition: str) -> list[str]:
    """Keyword fallback for find mode when no model is available."""
    stop = {"the", "a", "an", "they", "when", "if", "is", "are", "was", "of", "to", "and", "or", "in", "on",
            "it", "about", "talk", "talks", "say", "says", "said", "get", "gets", "their", "there", "where",
            "what", "who", "does", "do", "did", "any", "me", "wake", "tell"}
    words = [w for w in re.findall(r"[a-zA-Z0-9$']+", condition.lower()) if w not in stop and len(w) > 2]
    return words[:8]


def describe(plan: Plan) -> str:
    listen = ", ".join(f'"{c}"' for c in plan.cue_phrases[:4]) or "(semantic check only)"
    parts = [f"Listening for: {listen}"]
    if plan.ocr_patterns:
        parts.append("On-screen: " + ", ".join(f'"{p}"' for p in plan.ocr_patterns[:2]))
    if plan.visual_question:
        parts.append(f"Looking for: {plan.visual_question}")
    return " · ".join(parts)


def as_llm_input(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": text}]
