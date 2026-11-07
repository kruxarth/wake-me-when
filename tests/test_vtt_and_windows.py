from wake_me_when.wakeme import asr, vtt
from wake_me_when.wakeme.ingest import BYTES_PER_S, AudioRing

YT_AUTO = """WEBVTT
Kind: captions
Language: en

00:00:00.240 --> 00:00:02.150 align:start position:0%
so<00:00:00.480><c> how</c><00:00:00.640><c> much</c>

00:00:02.150 --> 00:00:02.160 align:start position:0%
so how much

00:00:02.160 --> 00:00:04.400 align:start position:0%
so how much
is<00:00:02.400><c> it</c>

00:00:04.400 --> 00:00:04.410
is it

00:00:04.410 --> 00:00:06.000
is it
seven ninety nine
"""


def test_youtube_rolling_captions_collapse_to_each_line_once():
    cues = vtt.parse(YT_AUTO)
    assert [c.text for c in cues] == ["so how much", "is it", "seven ninety nine"]
    assert cues[0].start == 0.24 and abs(cues[2].start - 4.41) < 1e-6


def test_srt_and_tags():
    srt = "1\n00:01:02,500 --> 00:01:04,000\n<i>Hello</i> &amp; welcome\n\n2\n00:01:05,000 --> 00:01:06,000\nNext\n"
    cues = vtt.parse(srt)
    assert cues[0].start == 62.5 and cues[0].text == "Hello & welcome"
    assert cues[1].text == "Next"


def test_demo_captions_parse(tmp_path):
    from wake_me_when.wakeme.sources import DEMO_DIR
    cues = vtt.parse((DEMO_DIR / "keynote.vtt").read_text())
    assert any("ninety nine dollars" in c.text for c in cues)


def test_each_segment_kept_by_exactly_one_overlapping_window():
    # windows start every 8 s and are 10 s long; a segment centred anywhere is owned once
    starts = [i * 8.0 for i in range(6)]
    for mid in [0.5, 7.9, 8.9, 9.1, 16.0, 17.0, 33.3, 47.9]:
        owners = [w for w in starts if asr.keep_segment(mid - 0.2, mid + 0.2, w, w == 0, w == starts[-1])]
        assert len(owners) == 1, (mid, owners)


def test_caption_windowing_matches_asr_windowing():
    cues = [vtt.Cue(t, t + 1, f"c{t}") for t in (0.5, 8.5, 9.5, 16.2, 30.0)]
    got = []
    for i, w in enumerate([0.0, 8.0, 16.0, 24.0]):
        got += [s.text for s in asr.captions_for(cues, w, i == 0, i == 3)]
    assert got == ["c0.5", "c8.5", "c9.5", "c16.2", "c30.0"]


def test_audio_ring_drops_oldest_and_tracks_position():
    ring = AudioRing(seconds=2)
    ring.push(b"\x01\x00" * (BYTES_PER_S // 2), 100.0)       # 1 s
    ring.push(b"\x02\x00" * (BYTES_PER_S // 2) * 2, 102.0)   # +2 s -> 1 s dropped
    assert abs(ring.start_pos - 1.0) < 1e-6 and abs(ring.end_pos - 3.0) < 1e-6
    assert abs(ring.dropped_s - 1.0) < 1e-6
    start, data = ring.read(0.0, 2.0)                        # asks for dropped audio
    assert start == 1.0 and len(data) == BYTES_PER_S
    assert abs(ring.wall_at(3.0) - 102.0) < 1e-6


def test_worker_queue_bounded_and_marks_behind():
    w = asr.Worker()
    for i in range(asr.MAX_QUEUE + 3):
        w.submit(asr.Job(1, i * 8.0, b"\x00\x00" * 100, i == 0, False, lambda s: None))
    assert w.backlog(1) == asr.MAX_QUEUE
    assert w.behind[1] is True
