"""Keyframe checks: cheap change detection, tesseract OCR, optional vision-model question."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

from .sources import which

VISION_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "boolean"}, "confidence": {"type": "number"}, "what": {"type": "string"}},
    "required": ["answer", "confidence", "what"],
}

VISION_INSTRUCTIONS = """WAKEME_VISION
Look at this frame from a live video stream and answer the yes/no question about it.
answer=true only if the frame clearly shows it. what: one short sentence describing what you see."""


class FrameGate:
    """Skip near-identical keyframes: a static slide or camera compresses to almost the same size."""

    def __init__(self, threshold: float = 0.04):
        self.threshold = threshold
        self.last_size: Optional[int] = None
        self.last_path: Optional[Path] = None

    def changed(self, path: Path) -> bool:
        if path == self.last_path:
            return False
        try:
            size = path.stat().st_size
        except OSError:
            return False
        self.last_path = path
        if self.last_size is None or abs(size - self.last_size) / max(1, self.last_size) > self.threshold:
            self.last_size = size
            return True
        return False


def ocr(path: Path, timeout: float = 15) -> str:
    exe = which("tesseract")
    if not exe:
        return ""
    try:
        p = subprocess.run([exe, str(path), "stdout", "--psm", "11"], capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return ""
    return " ".join(p.stdout.split()) if p.returncode == 0 else ""
