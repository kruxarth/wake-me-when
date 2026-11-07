"""Stage B judge (prompt, evidence mapping) and the confirm/debounce state machine."""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Optional

from .cues import normalize

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "matched": {"type": "boolean"},
        "confidence": {"type": "number"},
        "evidence_quote": {"type": "string"},
        "evidence_row_ids": {"type": "array", "items": {"type": "integer"}},
        "reason": {"type": "string"},
    },
    "required": ["matched", "confidence", "evidence_quote", "evidence_row_ids", "reason"],
}

JUDGE_INSTRUCTIONS = """WAKEME_JUDGE
You decide whether a watched-for moment has just happened in a live stream.
You get the condition, numbered transcript rows "[id] text" (speech recognition, may contain
mistakes), optional on-screen text, and moments the user already rejected.
Rules:
- matched=true only if the rows show the moment itself happening. Talking about it coming later,
  rumours, or a past mention is not the moment.
- Anything similar to a rejected moment is not a match.
- evidence_row_ids: the ids of the rows that prove it (1-3 rows). Never invent ids.
- evidence_quote: the shortest exact excerpt from those rows.
- confidence: 0..1, how sure you are the moment happened in these rows.
Reply with JSON only."""


def build_judge_input(condition: str, rows: list[tuple[int, str]], ocr_text: str = "",
                      negatives: list[str] | None = None, max_chars: int = 6000) -> str:
    lines = [f"[{rid}] {text}" for rid, text in rows]
    body = "\n".join(lines)
    if len(body) > max_chars:  # keep the newest rows
        body = body[-max_chars:]
        body = body[body.find("\n") + 1:]
    parts = [f"Condition: {condition}", "Transcript rows:", body or "(no speech)"]
    if ocr_text.strip():
        parts += ["On-screen text:", ocr_text.strip()[:600]]
    if negatives:
        parts += ["Rejected moments (NOT it):"] + [f"- {n[:200]}" for n in negatives[-5:]]
    return "\n".join(parts)


def map_evidence(result: dict, rows: list[tuple[int, str]]) -> list[int]:
    """Row ids the evidence rests on. Trust only ids that exist in the window; otherwise locate the
    quote in the window (the model is never trusted for times)."""
    window_ids = {rid for rid, _ in rows}
    ids = []
    for raw in result.get("evidence_row_ids") or []:
        try:
            rid = int(raw)
        except (TypeError, ValueError):
            continue
        if rid in window_ids and rid not in ids:
            ids.append(rid)
    if ids:
        return sorted(ids)
    quote = normalize(str(result.get("evidence_quote") or ""))
    if not quote:
        return []
    best, best_score = None, 0.0
    for rid, text in rows:
        nt = normalize(text)
        if quote in nt:
            return [rid]
        sm = SequenceMatcher(None, quote, nt)
        m = sm.find_longest_match(0, len(quote), 0, len(nt))
        score = m.size / max(1, len(quote))
        if score > best_score:
            best, best_score = rid, score
    return [best] if best is not None and best_score >= 0.5 else []


@dataclass
class Decision:
    action: str  # "fire" | "pending" | "none"
    row_ids: list[int] = field(default_factory=list)
    confidence: float = 0.0
    quote: str = ""
    unconfirmed: bool = False


class Debouncer:
    """Turns a stream of judge verdicts into fire decisions.

    * fire when confidence >= min_conf and (confidence >= 0.85 and confirm == "once", or two
      positives in a row);
    * the second positive must cite a row the first did not, or come from a window that reaches
      newer rows than the first one did (the judge looked again with more context and still agrees);
      re-judging the same window never counts twice;
    * evidence at or before the last fire is never used again (``--every`` cannot re-fire on it);
    * rows the user rejected ("not it") are suppressed.
    """

    HIGH = 0.85

    def __init__(self, min_conf: float = 0.6, confirm: str = "once"):
        self.min_conf = min_conf
        self.confirm = confirm
        self.pending: Optional[set[int]] = None
        self.pending_newest = 0  # newest row id in the window that produced the pending positive
        self.last_fire_row = 0
        self.suppressed: set[int] = set()

    def judge(self, matched: bool, confidence: float, row_ids: list[int], quote: str = "",
              newest_row: int = 0) -> Decision:
        """``newest_row``: the newest row id in the judged window (0 when unknown)."""
        ids = {r for r in row_ids if r > self.last_fire_row and r not in self.suppressed}
        if not matched or confidence < self.min_conf or not ids:
            self.pending, self.pending_newest = None, 0
            return Decision("none")
        if confidence >= self.HIGH and self.confirm != "twice":
            return self._fire(ids, confidence, quote)
        if self.pending is not None and (ids - self.pending or newest_row > self.pending_newest):
            return self._fire(ids | self.pending, confidence, quote)
        self.pending = ids if self.pending is None else self.pending | ids
        self.pending_newest = max(self.pending_newest, newest_row)
        return Decision("pending", sorted(ids), confidence, quote)

    def stage_a_only(self, strong: bool, row_ids: list[int], quote: str = "") -> Decision:
        """Fallback when the judge is unavailable: fire only on strong cue evidence, marked unconfirmed."""
        ids = {r for r in row_ids if r > self.last_fire_row and r not in self.suppressed}
        if strong and ids:
            d = self._fire(ids, 0.0, quote)
            d.unconfirmed = True
            return d
        return Decision("none")

    def _fire(self, ids: set[int], confidence: float, quote: str) -> Decision:
        self.pending = None
        self.pending_newest = 0
        self.last_fire_row = max(ids)
        return Decision("fire", sorted(ids), confidence, quote)

    def reject(self, row_ids: list[int]) -> None:
        """User said "not it": these rows never count again; the watch resumes from here."""
        self.suppressed.update(row_ids)
        self.pending, self.pending_newest = None, 0


def parse_verdict(parsed: Any) -> tuple[bool, float, str]:
    if not isinstance(parsed, dict):
        return False, 0.0, ""
    try:
        conf = float(parsed.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    conf = min(max(conf, 0.0), 1.0)
    return bool(parsed.get("matched")), conf, str(parsed.get("evidence_quote") or "")[:300]
