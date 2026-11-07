"""Stage A: free, local cue matching on new transcript text (fuzzy + phonetic) and OCR regexes."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

_WORD = re.compile(r"[a-z0-9$%']+")


def normalize(text: str) -> str:
    text = text.lower().replace("-", " ").replace(",", "")
    return " ".join(_WORD.findall(text))


def soundex(word: str) -> str:
    """Classic Soundex: enough to catch ASR spellings like 'Nvidia'/'envidia', 'Kubernetes'/'cooper netties'."""
    word = re.sub(r"[^a-z]", "", word.lower())
    if not word:
        return ""
    codes = {**dict.fromkeys("bfpv", "1"), **dict.fromkeys("cgjkqsxz", "2"), **dict.fromkeys("dt", "3"),
             "l": "4", **dict.fromkeys("mn", "5"), "r": "6"}
    out, last = word[0].upper(), codes.get(word[0], "")
    for ch in word[1:]:
        code = codes.get(ch, "")
        if code and code != last:
            out += code
        if ch not in "hw":
            last = code
    return (out + "000")[:4]


def stem(word: str) -> str:
    """Crude suffix stripping: 'holding'/'held' is not solved, but 'holding'/'hold', 'prices'/'price' are."""
    for suf in ("ing", "ed", "es", "s"):
        if len(word) > len(suf) + 3 and word.endswith(suf):
            return word[: -len(suf)]
    return word


def _token_match(a: str, b: str) -> bool:
    if a == b or stem(a) == stem(b):
        return True
    if len(a) >= 4 and len(b) >= 4 and (SequenceMatcher(None, a, b).ratio() >= 0.8):
        return True
    return (len(a) >= 5 and len(b) >= 5 and soundex(a) == soundex(b) and a[:2] == b[:2]
            and SequenceMatcher(None, a, b).ratio() >= 0.75)


def phrase_in(phrase: str, text: str) -> bool:
    """Is ``phrase`` (normalised) present in ``text`` (normalised), allowing ASR-level fuzz per token."""
    p, t = phrase.split(), text.split()
    if not p:
        return False
    if f" {' '.join(p)} " in f" {' '.join(t)} ":
        return True
    n = len(p)
    for i in range(0, max(0, len(t) - n) + 1):
        if all(_token_match(pa, ta) for pa, ta in zip(p, t[i:i + n])):
            return True
    return False


@dataclass
class StageAResult:
    cues: list[str] = field(default_factory=list)
    ocr: list[str] = field(default_factory=list)
    negatives: list[str] = field(default_factory=list)
    row_ids: list[int] = field(default_factory=list)

    @property
    def hit(self) -> bool:
        return bool(self.cues or self.ocr)

    @property
    def strong(self) -> bool:
        """Good enough to alert without the judge (LLM cap exhausted or provider down)."""
        return (len(self.cues) >= 2 or bool(self.ocr)) and not self.negatives


def match(rows: list[tuple[int, str]], cue_phrases: list[str], negative_cues: list[str] | None = None,
          ocr_text: str = "", ocr_patterns: list[str] | None = None) -> StageAResult:
    res = StageAResult()
    norm_cues = [(c, normalize(c)) for c in cue_phrases if normalize(c)]
    norm_neg = [(c, normalize(c)) for c in (negative_cues or []) if normalize(c)]
    for rid, text in rows:
        nt = normalize(text)
        hit_here = False
        for raw, nc in norm_cues:
            if phrase_in(nc, nt):
                if raw not in res.cues:
                    res.cues.append(raw)
                hit_here = True
        for raw, nc in norm_neg:
            if phrase_in(nc, nt) and raw not in res.negatives:
                res.negatives.append(raw)
        if hit_here:
            res.row_ids.append(rid)
    for pat in ocr_patterns or []:
        try:
            if ocr_text and re.search(pat, ocr_text, re.I):
                res.ocr.append(pat)
        except re.error:
            if pat.lower() in ocr_text.lower():
                res.ocr.append(pat)
    return res
