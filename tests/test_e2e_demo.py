"""End to end: a real ffmpeg ingest of demo://keynote?speed=4 with a stubbed LLM, no network."""

import json
import os
import subprocess
import time

import pytest
import yaml

from conftest import ROOT, FakeCtx, FakeLLM, needs_ffmpeg
from wake_me_when.wakeme import sources
from wake_me_when.wakeme.app import App
from wake_me_when.wakeme.store import Store

PRICE = json.loads((sources.DEMO_DIR / "demos.json").read_text())["keynote"]["moments"]["price"]
SESSION = {"session_key": "agent:main:telegram:dm:1", "session_id": "s1", "platform": "telegram"}


def _wait(pred, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.2)
    return False


def _ffmpeg_children():
    # only real ffmpeg processes (-x: process name), so a shell whose command line mentions the file never counts
    out = subprocess.run(["pgrep", "-a", "-x", "ffmpeg"], capture_output=True, text=True).stdout.splitlines()
    return [line for line in out if "keynote" in line]


@needs_ffmpeg
def test_demo_watch_alerts_clips_catches_up_and_cleans_up(tmp_path):
    ctx = FakeCtx()
    app = App(ctx, Store(), {"wakeme_judge"})
    t0 = time.time()
    reply = app.watch("demo://keynote?speed=4 when they announce the price", SESSION)
    assert reply.startswith("👀 Watch #1 started · planning…")
    assert time.time() - t0 < 1.0                       # instant: no network, no model before the reply

    assert _wait(lambda: any(t.startswith("📋 Watch #1") for t in ctx.texts()), 20)
    assert _wait(lambda: any(t.startswith("🔔") for t in ctx.texts()), 60), ctx.texts()
    # at 4x the price (0:52 of media) is reached ~13 s in; a real-time run would need 52+ s
    assert time.time() - t0 < 35, time.time() - t0
    alert = next(t for t in ctx.texts() if t.startswith("🔔"))
    assert "starts at just seven hundred" in alert
    assert alert.rstrip().endswith("Reply “clip”, “not it” or “stop”.") and "/wakeme" not in alert
    assert "0:52" in alert                              # stream time comes from the row, not the model

    # alert latency: how long after the price audio reached ffmpeg did the alert go out
    ev = app.store.events(1, "alert")[0]
    assert ev["latency_from_arrival"] < 30
    w = app.mgr.get(1)
    assert w.status == "fired"
    # the judge only ever saw the plugin's own aux task
    assert all(task == "wakeme_judge" for kind, name, task in ctx.llm.calls if name.startswith("WAKEME"))

    # P1 auto-attach asked the agent (via inject_message, refused here: no grant) for this chat only
    assert _wait(lambda: ctx.injected, 30)
    assert ctx.injected[0][0] == SESSION["session_key"] and "wakeme_clip" in ctx.injected[0][1]

    path, msg = app.clip("1")
    assert path is not None and path.exists() and "ninety nine" in msg
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(path)], capture_output=True, text=True).stdout)
    assert dur >= w.s.clip_seconds - 1, dur             # the whole window, not cut off after the moment

    summary = app.catchup("1")
    assert summary.startswith("📺 Catch-up · watch #1")

    listing = app.watching(SESSION)
    assert "#1 · Acme Phone X" in listing and "fired" in listing

    hint = app.context_for(session_id="s1")
    assert "#1" in hint and "they announce the price" in hint
    assert "Watch #1 alerted most recently" in hint and "wakeme_feedback" in hint and "watch_id 1" in hint

    assert _wait(lambda: w.ingest and w.ingest.proc and w.ingest.proc.poll() is not None, 30)  # media over:
    time.sleep(2)                                       # the watch now waits in its post-alert grace window
    assert app.stop("1") == "⏹ Stopped watch #1."
    assert _wait(lambda: app.store.watch(1)["status"] == "stopped", 10), app.store.watch(1)["status"]
    assert not any(t.startswith("🏁") for t in ctx.texts())   # a user stop is not "the stream ended"
    assert _wait(lambda: not _ffmpeg_children(), 10), _ffmpeg_children()


@needs_ffmpeg
def test_demo_speed_really_paces_faster_than_real_time(tmp_path):
    from wake_me_when.wakeme.ingest import AudioRing, Ingest
    src = sources.resolve("demo://keynote?speed=4", "demo")
    ring = AudioRing()
    ing = Ingest(src, tmp_path, frames=False, ring=ring)
    ing.start()
    time.sleep(8)
    ing.stop()
    assert 24 <= ring.end_pos <= 40, ring.end_pos     # ~32 s of media in 8 s
    assert ing.segments()                              # the clip ring filled too


@needs_ffmpeg
def test_not_it_resumes_and_negative_example_reaches_judge():
    seen = []

    def judge(text):
        seen.append(text)
        if "Rejected moments" in text:
            return {"matched": False, "confidence": 0.2, "evidence_quote": "", "evidence_row_ids": [], "reason": "x"}
        import re
        rid = re.findall(r"^\[(\d+)\] .*waiting to hear", text, re.M)
        if rid:
            return {"matched": True, "confidence": 0.9, "evidence_quote": "waiting to hear",
                    "evidence_row_ids": [int(rid[0])], "reason": "tease"}
        return {"matched": False, "confidence": 0.1, "evidence_quote": "", "evidence_row_ids": [], "reason": "no"}

    ctx = FakeCtx(llm=FakeLLM(judge_fn=judge, plan={"summary": "s", "cue_phrases": ["waiting to hear", "dollars"],
                                                    "ocr_patterns": [], "visual_question": None,
                                                    "negative_cues": [], "confirm": "once",
                                                    "min_confidence": 0.6}))
    app = App(ctx, Store(), {"wakeme_judge"})
    app.watch("demo://keynote?speed=4 when they announce the price", SESSION)
    assert _wait(lambda: any(t.startswith("🔔") for t in ctx.texts()), 60)
    assert "waiting to hear" in next(t for t in ctx.texts() if t.startswith("🔔"))
    assert app.not_it("1", "that was a tease").startswith("👍")
    assert app.mgr.get(1).status == "watching"
    assert _wait(lambda: any("Rejected moments" in s and "tease" in s for s in seen), 40)
    app.stop("1")


@needs_ffmpeg
def test_llm_outage_falls_back_to_unconfirmed_cue_alerts():
    ctx = FakeCtx(llm=FakeLLM())
    app = App(ctx, Store(), set())
    app.watch("demo://keynote?speed=4 when they announce the price", SESSION)
    assert _wait(lambda: any(t.startswith("📋") for t in ctx.texts()), 20)
    ctx.llm.fail = True                                   # provider goes down after planning
    assert _wait(lambda: any(t.startswith("🔔") for t in ctx.texts()), 60), ctx.texts()
    assert "unconfirmed" in next(t for t in ctx.texts() if t.startswith("🔔"))
    app.stop("1")


def test_plan_failure_ends_watch_with_reason():
    ctx = FakeCtx(llm=FakeLLM(fail=True))
    app = App(ctx, Store(), set())
    app.watch("demo://keynote when they announce the price", SESSION)
    assert _wait(lambda: any(t.startswith("❌ Watch #1 couldn't plan") for t in ctx.texts()), 30), ctx.texts()
    assert app.store.watch(1)["status"] == "ended"


def test_max_watches_counts_every_live_watch():
    ctx = FakeCtx(settings={"max_watches": 1})
    store = Store()
    other = store.create_watch("demo://launch", "demo", "x", "once", "", "", "telegram")
    store.update_watch(other, status="watching", owner=f"{os.getppid()}:")  # a live watch of another process
    app = App(ctx, store, set())
    assert "max_watches" in app.watch("demo://keynote when they announce the price", SESSION)


def test_orphans_marked_stopped_on_load():
    store = Store()
    wid = store.create_watch("demo://launch", "demo", "x", "once", "", "", "telegram")
    store.update_watch(wid, status="watching", owner="999999999:1")
    assert store.mark_orphans() == 1
    row = store.watch(wid)
    assert row["status"] == "stopped" and row["note"] == "gateway restart"


def test_register_against_fake_ctx_matches_manifest():
    import wake_me_when
    ctx = FakeCtx()
    wake_me_when.register(ctx)
    manifest = yaml.safe_load((ROOT / "plugin.yaml").read_text())
    assert sorted(ctx.tools) == sorted(manifest["provides_tools"])
    assert sorted(ctx.hooks) == sorted(manifest["provides_hooks"])
    assert set(ctx.commands) == {"wakeme"}                # one slash command; list/catchup are subcommands
    assert "wake-me-when" in ctx.skills and "wakeme" in ctx.cli
    assert set(ctx.tasks) == {"wakeme_judge", "wakeme_vision"}
    assert set(manifest["config_schema"]) == set(__import__("wake_me_when.wakeme.config", fromlist=["SCHEMA"]).SCHEMA)
    # every tool handler returns JSON, never raises, even on garbage input
    for name, (schema, handler) in ctx.tools.items():
        if name in {"wakeme_watch", "wakeme_find"}:
            continue
        json.loads(handler({"watch_id": "nope"}).split("\nMEDIA:")[0])
    assert ctx.commands["wakeme"]("").startswith("Wake Me When")
    assert ctx.hooks["pre_llm_call"](session_id="none") is None
    for cb in ctx.unload:
        cb()


def test_gateway_chat_cannot_watch_local_files(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x")
    app = App(FakeCtx(), Store(), set())
    assert "allowed_media_dirs" in app.watch(f"{f} when anything", SESSION)
    pytest.importorskip("yaml")


@needs_ffmpeg
def test_pipe_source_runs_in_one_process_group_and_dies_together(tmp_path):
    """Twitch path: streamlink | ffmpeg share a process group (regression: EPERM across sessions)."""
    from wake_me_when.wakeme.ingest import AudioRing, Ingest
    src = sources.Source(url="https://twitch.tv/x", kind="twitch", inputs=["pipe:0"],
                         whitelist=sources.PIPE_WHITELIST,
                         pipe_argv=["cat", str(sources.DEMO_DIR / "launch.mp4")])
    ring = AudioRing()
    ing = Ingest(src, tmp_path, frames=False, ring=ring)
    ing.start()
    assert _wait(lambda: ring.end_pos > 5, 15)
    pg = os.getpgid(ing.proc.pid)
    assert os.getpgid(ing.pipe.pid) == pg
    ing.stop()
    left = subprocess.run(["pgrep", "-g", str(pg)], capture_output=True, text=True).stdout.strip()
    assert left == ""


@needs_ffmpeg
def test_plan_model_turns_on_repeat_mode_and_card_is_plain():
    ctx = FakeCtx(llm=FakeLLM(plan={"summary": "camera mentioned", "cue_phrases": ["camera"],
                                    "ocr_patterns": [r"\bcam(era)?\b"], "screen_label": "the word camera",
                                    "visual_question": None, "negative_cues": [], "confirm": "once",
                                    "min_confidence": 0.6, "mode": "every", "cooldown_min": 2}))
    app = App(ctx, Store(), {"wakeme_judge"})
    app.watch("demo://keynote?speed=4 anytime the camera comes up", SESSION)  # "anytime": regex catches it too
    app.watch("demo://launch?speed=4 the camera comes up", SESSION)           # only the model sees "repeat"
    assert _wait(lambda: sum(t.startswith("📋") for t in ctx.texts()) == 2, 20), ctx.texts()
    card = next(t for t in ctx.texts() if t.startswith("📋 Watch #2"))
    assert "Alerting every time, at most once every 2 min." in card
    assert "Watching the screen for: the word camera" in card or "tesseract" in card
    assert "\\b" not in card
    assert app.mgr.get(2).req.mode == "every" and app.store.watch(2)["mode"] == "every"
    assert app.mgr.get(1).req.cooldown_min == 5                               # the user's words win
    app.stop("all")
