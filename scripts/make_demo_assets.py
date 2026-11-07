#!/usr/bin/env python3
"""Generate the bundled synthetic demo streams (assets/demo/*.mp4 + *.vtt + demos.json).

Everything here is original: the scripts below are written for this plugin, the speech is
synthesised with edge-tts, and the slides are ffmpeg drawtext on a flat background. Run once by a
maintainer; the outputs are committed so reviewers and tests need no network.

    pip install "edge-tts>=7,<8"
    python scripts/make_demo_assets.py

Needs ffmpeg/ffprobe on PATH and a TrueType font (override with --font).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "demo"
RATE = 24000
GAP_S = 0.7

# 10 fps, no B-frames: ffmpeg -readrate (demo ?speed=) mis-paces 5 fps video with stream copy.
# (slide text, spoken line, moment key or "")
KEYNOTE = [
    ("ACME LAUNCH 2026", "Good morning everyone, and welcome to the Acme launch event. It is great to have you here, and online, with us today.", ""),
    ("ACME LAUNCH 2026", "Today we are going to talk about three things. The design, the camera, and of course, when you can get one.", ""),
    ("DESIGN", "Let's start with design. Acme Phone X is milled from a single block of recycled aluminium.", ""),
    ("DESIGN", "It is thinner than last year's model, and it is lighter too, at just one hundred and seventy grams.", ""),
    ("CAMERA", "Now, the camera. We built a new periscope lens with five times optical zoom.", ""),
    ("CAMERA", "Low light photos are twice as bright, and video stabilisation finally works when you run.", ""),
    ("BATTERY", "Battery life is up to two full days. And charging only costs you twenty minutes of your morning.", "near_miss"),
    ("BATTERY", "I know what many of you are waiting to hear. We will get to that in just a moment.", "near_miss"),
    ("ACME PHONE X", "So, how much is it? Acme Phone X starts at just seven hundred and ninety nine dollars.", "price"),
    ("$799", "That is seven ninety nine for the base model, with one hundred and twenty eight gigabytes of storage.", "price_repeat"),
    ("PRE-ORDER FRIDAY", "Pre-orders open this Friday, and it ships on November twentieth.", "ship_date"),
    ("THANK YOU", "Thank you all for joining us. Now let's go take some photos.", ""),
]

LAUNCH = [
    ("T-10:00", "This is launch control. We are at T minus ten minutes and counting for the Starhopper Two test flight.", ""),
    ("T-08:00", "All stations report ready. Propellant loading is complete and the vehicle is on internal power.", ""),
    ("T-05:00 HOLD", "We are now holding at T minus five minutes. Range safety has reported a boat in the hazard area.", "hold"),
    ("T-05:00 HOLD", "The hold is expected to last a few minutes while the range is cleared. Stand by.", "hold"),
    ("T-05:00 HOLD", "Range is now clear. Weather remains go. We will resume the count shortly.", ""),
    ("T-05:00", "And we have resumed the count. T minus five minutes and counting.", "resume"),
    ("T-02:00", "T minus two minutes. Flight computer is in startup.", ""),
    ("T-01:00", "T minus one minute. All systems are go for launch.", "t_minus_1"),
    ("T-00:10", "Ten, nine, eight, seven, six, five, four, three, two, one.", ""),
    ("LIFTOFF", "Liftoff! We have liftoff of Starhopper Two.", "liftoff"),
    ("ASCENT", "The vehicle has cleared the tower and is climbing on a good trajectory.", ""),
]

DEMOS = {
    "keynote": {"title": "Acme Phone X launch keynote (synthetic demo)", "voice": "en-US-AndrewNeural",
                "lines": KEYNOTE, "bg": "0x14213d"},
    "launch": {"title": "Starhopper Two launch coverage (synthetic demo)", "voice": "en-US-GuyNeural",
               "lines": LAUNCH, "bg": "0x0b1d14"},
}


def run(argv: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(argv, check=True, capture_output=True, **kw)


async def tts(text: str, voice: str, path: Path) -> None:
    import edge_tts  # generation-time only; not a plugin dependency

    for attempt in range(5):
        try:
            await edge_tts.Communicate(text, voice).save(str(path))
            return
        except edge_tts.exceptions.NoAudioReceived:
            await asyncio.sleep(2 + attempt * 3)
    raise RuntimeError(f"edge-tts returned no audio for: {text[:40]}")


def pcm(path: Path) -> bytes:
    return run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1", "-ar", str(RATE),
                "-f", "s16le", "pipe:1"]).stdout


def vtt_ts(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def esc(text: str) -> str:
    # drawtext text= escaping (inside a single-quoted filter value)
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "’").replace("%", "\\%")


def build(name: str, spec: dict, font: str, tmp: Path) -> dict:
    audio = bytearray()
    cues, moments = [], {}
    gap = b"\x00\x00" * int(RATE * GAP_S)
    audio += gap
    for i, (slide, line, key) in enumerate(spec["lines"]):
        mp3 = tmp / f"{name}_{i}.mp3"
        asyncio.run(tts(line, spec["voice"], mp3))
        start = len(audio) / 2 / RATE
        audio += pcm(mp3)
        end = len(audio) / 2 / RATE
        audio += gap
        cues.append({"start": round(start, 3), "end": round(end, 3), "slide": slide, "text": line})
        if key and key not in moments:
            moments[key] = round(start, 3)
    total = len(audio) / 2 / RATE + 1.0
    raw = tmp / f"{name}.pcm"
    raw.write_bytes(bytes(audio))

    # slide i is shown from its cue start until the next cue starts
    draws = []
    for i, c in enumerate(cues):
        a = 0.0 if i == 0 else c["start"]
        b = cues[i + 1]["start"] if i + 1 < len(cues) else total
        draws.append(f"drawtext=fontfile='{font}':text='{esc(c['slide'])}':fontsize=64:fontcolor=white:"
                     f"x=(w-text_w)/2:y=(h-text_h)/2-10:enable='between(t,{a:.2f},{b:.2f})'")
    draws.append(f"drawtext=fontfile='{font}':text='SYNTHETIC DEMO STREAM':fontsize=16:"
                 f"fontcolor=0xffffff99:x=16:y=h-30")
    out = OUT / f"{name}.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c={spec['bg']}:s=640x360:r=10:d={total:.2f}",
         "-f", "s16le", "-ar", str(RATE), "-ac", "1", "-i", str(raw),
         "-vf", ",".join(draws), "-c:v", "libx264", "-preset", "slow", "-crf", "30", "-g", "50", "-bf", "0",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "40k", "-shortest", "-movflags", "+faststart",
         str(out)])

    lines = ["WEBVTT", ""]
    for c in cues:
        lines += [f"{vtt_ts(c['start'])} --> {vtt_ts(c['end'])}", c["text"], ""]
    (OUT / f"{name}.vtt").write_text("\n".join(lines), encoding="utf-8")
    return {"title": spec["title"], "duration": round(total, 2), "moments": moments}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--font", default="/usr/share/fonts/noto/NotoSans-Bold.ttf")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    meta = {}
    with tempfile.TemporaryDirectory() as td:
        for name, spec in DEMOS.items():
            meta[name] = build(name, spec, args.font, Path(td))
            print(name, meta[name])
    (OUT / "demos.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
