"""WebVTT / SRT parsing, including YouTube auto-captions (rolling, duplicated lines, inline tags)."""

from __future__ import annotations

import re
from dataclasses import dataclass

_TS = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")
_CUE = re.compile(rf"^\s*({_TS.pattern})\s*-->\s*({_TS.pattern})")
_TAG = re.compile(r"<[^>]+>")


@dataclass
class Cue:
    start: float
    end: float
    text: str


def _secs(m: re.Match) -> float:
    h, mnt, s, frac = m.groups()
    return int(h or 0) * 3600 + int(mnt) * 60 + int(s) + int(frac.ljust(3, "0")) / 1000


def parse(text: str) -> list[Cue]:
    """Parse VTT or SRT text into cues with clean text.

    YouTube auto-captions repeat each line across two cues ("rolling" captions) and carry
    per-word timing tags; both are collapsed so every spoken line appears once, at its first time.
    """
    cues: list[Cue] = []
    block: list[str] = []
    times: tuple[float, float] | None = None

    def flush() -> None:
        if times is None:
            return
        body = " ".join(_TAG.sub("", line).strip() for line in block if line.strip())
        body = re.sub(r"\s+", " ", body).replace("&amp;", "&").replace("&gt;", ">").replace("&lt;", "<").strip()
        if body:
            cues.append(Cue(times[0], times[1], body))

    for raw in text.replace("\r", "").split("\n"):
        line = raw.rstrip("\n")
        m = _CUE.match(line)
        if m:
            flush()
            ts = list(_TS.finditer(line))
            times = (_secs(ts[0]), _secs(ts[1]))
            block = []
            continue
        if not line.strip():
            flush()
            times, block = None, []
            continue
        if times is not None:
            block.append(line)
    flush()
    return _dedupe_rolling(cues)


def _dedupe_rolling(cues: list[Cue]) -> list[Cue]:
    """Drop the repeated half of rolling captions: keep only text not already shown."""
    out: list[Cue] = []
    seen_tail = ""
    for c in cues:
        text = c.text
        if seen_tail and text.startswith(seen_tail):
            text = text[len(seen_tail):].strip()
        if out and text == out[-1].text:
            out[-1].end = max(out[-1].end, c.end)
            continue
        if text:
            out.append(Cue(c.start, c.end, text))
            seen_tail = text
        else:
            out[-1].end = max(out[-1].end, c.end) if out else c.end
    return out
