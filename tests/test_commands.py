"""The single /wakeme command: aliases, routing of `/wakeme <url> <question>`, optional ids."""

from types import SimpleNamespace

import pytest

from conftest import FakeCtx
from wake_me_when.wakeme import commands, sources
from wake_me_when.wakeme.app import HELP, App
from wake_me_when.wakeme.store import Store

SESSION = dict(session_key="agent:main:telegram:dm:1", session_id="s1", platform="telegram")


@pytest.fixture
def app(monkeypatch):
    a = App(FakeCtx(), Store(), set())
    calls = []
    monkeypatch.setattr(a, "start_watch", lambda req, session: calls.append(("watch", req)) or "👀 Watch #1 started")
    monkeypatch.setattr(a, "find", lambda raw, session: calls.append(("find", raw)) or "🔎 found")
    monkeypatch.setattr(a, "not_it", lambda target, note="": calls.append(("wrong", target, note)) or "👍")
    monkeypatch.setattr(a, "watching", lambda session=None: calls.append(("list",)) or "👀 Watching (0)")
    monkeypatch.setattr(a, "catchup", lambda raw: calls.append(("catchup", raw)) or "📺")
    a.calls = calls
    return a


def run(app, text):
    return commands.wakeme(app)(text, **SESSION)


def test_help_is_the_documented_four_lines():
    lines = HELP.splitlines()
    assert lines[1].startswith("/wakeme <url> when <condition>")
    assert lines[2].startswith("/wakeme <recording url> <question>")
    assert lines[3] == "/wakeme list · /wakeme catchup [id] · /wakeme clip [id] · /wakeme stop [id|all]"
    assert lines[4].startswith("After an alert, just reply: clip · not it · stop")
    assert "notit" not in HELP and "/watching" not in HELP and "/catchup" not in HELP and "--every" not in HELP


@pytest.mark.parametrize("text", ["list", "status", "watching"])
def test_list_aliases(app, text):
    assert run(app, text) == "👀 Watching (0)"


@pytest.mark.parametrize("text,target,note", [
    ("wrong", "", ""), ("wrong 2", "2", ""), ("notit 2 a tease", "2", "a tease"), ("nope", "", ""),
    ("not it", "", ""), ("not-it #3", "#3", ""), ("wrong that was a tease", "", "that was a tease"),
])
def test_wrong_aliases(app, text, target, note):
    run(app, text)
    assert app.calls[-1] == ("wrong", target, note)


def test_catchup_subcommand(app):
    run(app, "catchup 1 10")
    assert app.calls[-1] == ("catchup", "1 10")


def test_question_on_a_recording_searches(app, tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "check_url", lambda url, **kw: "youtube")
    monkeypatch.setattr(sources, "ytdlp_info", lambda url, **kw: {"is_live": False, "live_status": "not_live"})
    assert run(app, "https://youtu.be/abc where do they talk about the battery?") == "🔎 found"
    assert app.calls[-1][0] == "find"
    run(app, "https://youtu.be/abc when they talk about the battery")       # "when …" always watches
    assert app.calls[-1][0] == "watch"


def test_question_on_a_live_stream_watches(app, monkeypatch):
    monkeypatch.setattr(sources, "check_url", lambda url, **kw: "youtube")
    monkeypatch.setattr(sources, "ytdlp_info", lambda url, **kw: {"is_live": True})
    run(app, "https://youtu.be/abc where do they talk about the battery")
    assert app.calls[-1][0] == "watch"
    run(app, "demo://keynote what time do they announce the price?")       # demo:// is a stream to watch
    assert app.calls[-1][0] == "watch"
    run(app, "https://www.youtube.com/@NASA/live what is happening")       # /live: no network probe needed
    assert app.calls[-1][0] == "watch"


def test_unclear_wording_on_a_recording_watches_and_mentions_find(app):
    reply = run(app, "https://www.youtube.com/watch?v=abc the price")
    assert app.calls[-1][0] == "watch" and "/wakeme find" in reply
    assert "/wakeme find" not in run(app, "demo://keynote the price")


def test_explicit_find_still_works(app):
    assert run(app, "find demo://keynote the price") == "🔎 found"


def test_stop_without_id_uses_the_only_watch(monkeypatch):
    a = App(FakeCtx(), Store(), set())
    assert a.stop("") == "No watch is running in this Hermes. /wakeme list shows them."
    a.mgr.watches[4] = SimpleNamespace(id=4, req=SimpleNamespace(condition="x"))
    monkeypatch.setattr(a.mgr, "stop", lambda wid: f"⏹ Stopped watch #{wid}.")
    assert a.stop("") == "⏹ Stopped watch #4."


def test_tool_reads_repeat_words_from_the_condition(monkeypatch):
    from wake_me_when.wakeme import tools
    a = App(FakeCtx(), Store(), set())
    seen = []
    monkeypatch.setattr(a, "start_watch", lambda req, session: seen.append(req) or "ok")
    tools.handlers(a)["wakeme_watch"]({"url": "demo://keynote", "condition": "every time they mention the camera"})
    assert seen[-1].mode == "every" and seen[-1].condition == "they mention the camera"
    tools.handlers(a)["wakeme_watch"]({"url": "demo://keynote", "condition": "they say price"})
    assert seen[-1].mode == "once" and not seen[-1].mode_set
