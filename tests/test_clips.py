import json
import shutil
import subprocess

from conftest import needs_ffmpeg
from wake_me_when.wakeme import clips, sources


def _segment_demo(tmp_path):
    """Cut the demo into a 5 s segment ring, as ingest does (stream copy)."""
    seg = tmp_path / "seg"
    seg.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-protocol_whitelist", "file", "-i",
                    str(sources.DEMO_DIR / "keynote.mp4"), "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
                    "-c:a", "aac", "-f", "segment", "-segment_time", "5", "-segment_list", str(seg / "list.csv"),
                    "-segment_list_type", "csv", str(seg / "s_%06d.ts")], check=True)
    rows = [l.split(",") for l in (seg / "list.csv").read_text().splitlines() if l]
    return [(str(seg / r[0]), float(r[1]), float(r[2])) for r in rows]


def _duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout
    return float(out)


@needs_ffmpeg
def test_clip_after_ring_rotated_past_the_moment(tmp_path):
    segs = _segment_demo(tmp_path)
    price = json.loads((sources.DEMO_DIR / "demos.json").read_text())["keynote"]["moments"]["price"]
    evidence = tmp_path / "evidence" / "a1"
    saved = clips.snapshot(segs, evidence, price - 12, price + 12)
    assert saved and all(s[1] < price + 12 and s[2] > price - 12 for s in saved)
    # the ring rotates: every original segment is deleted
    shutil.rmtree(tmp_path / "seg")
    out = clips.cut(shutil.which("ffmpeg"), clips.load_snapshot(evidence), price - 6, 18, tmp_path / "clip.mp4")
    assert out is not None and out.exists()
    assert 14 <= _duration(out) <= 19


@needs_ffmpeg
def test_snapshot_merges_later_segments(tmp_path):
    segs = _segment_demo(tmp_path)
    ev = tmp_path / "ev"
    first = clips.snapshot(segs[:3], ev, 0, 40)
    merged = clips.snapshot(segs[:6], ev, 0, 40)
    assert len(merged) > len(first)
    assert [s[1] for s in merged] == sorted(s[1] for s in merged)


@needs_ffmpeg
def test_cut_without_segments_returns_none(tmp_path):
    assert clips.cut(shutil.which("ffmpeg"), [], 0, 10, tmp_path / "x.mp4") is None


def test_snapshot_survives_unreadable_index_and_concurrent_writers(tmp_path):
    # Telegram run: resnap read index.json while the auto-attach clip was rewriting it -> JSONDecodeError
    # killed the watch. An empty/partial index now counts as no copies, and writes are atomic.
    import threading

    ring = tmp_path / "ring"
    ring.mkdir()
    segs = []
    for i in range(6):
        p = ring / f"s_{i:06d}.ts"
        p.write_bytes(b"x" * 1000)
        segs.append((str(p), i * 5.0, i * 5.0 + 5))
    dest = tmp_path / "ev"
    dest.mkdir()
    (dest / "index.json").write_text("")
    assert len(clips.snapshot(segs[:2], dest, 0, 10)) == 2

    errors = []

    def hammer():
        try:
            for _ in range(200):
                clips.snapshot(segs, dest, 0, 30)
        except Exception as e:  # pragma: no cover - the failure mode under test
            errors.append(e)

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(clips.load_snapshot(dest)) == 6
