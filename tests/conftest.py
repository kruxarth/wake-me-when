"""Load the plugin directory (hyphenated, so not importable by name) as package ``wake_me_when``."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_package():
    if "wake_me_when" in sys.modules:
        return sys.modules["wake_me_when"]
    spec = importlib.util.spec_from_file_location("wake_me_when", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wake_me_when"] = mod
    spec.loader.exec_module(mod)
    return mod


_load_package()

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setenv("WAKEME_DATA_DIR", str(d))
    return d


class FakeLLM:
    """Deterministic stand-in for ctx.llm: plans from keywords, judges on explicit price/liftoff rows."""

    def __init__(self, judge_fn=None, plan=None, fail=False):
        self.calls = []
        self.judge_fn = judge_fn
        self.plan = plan
        self.fail = fail

    def complete_structured(self, *, instructions, input, json_schema=None, **kw):
        text = "\n".join(p.get("text", "") for p in input if p.get("type") == "text")
        self.calls.append(("structured", instructions.split("\n", 1)[0], kw.get("task")))
        if self.fail:
            raise RuntimeError("provider 500")
        if instructions.startswith("WAKEME_PLAN"):
            parsed = self.plan or {"summary": "price announced", "cue_phrases": ["price", "dollars", "starts at"],
                                   "ocr_patterns": [], "visual_question": None, "negative_cues": ["in a moment"],
                                   "confirm": "once", "min_confidence": 0.6}
        elif instructions.startswith("WAKEME_JUDGE"):
            parsed = (self.judge_fn or default_judge)(text)
        elif instructions.startswith("WAKEME_FIND"):
            import re
            ids = [int(m) for m in re.findall(r"^\[(\d+)\] \([^)]*\) .*(?:dollars|seven ninety)", text, re.M)]
            parsed = {"results": [{"passage_id": i, "score": 0.9, "evidence": "starts at just seven hundred "
                                   "and ninety nine dollars"} for i in ids[:2]]}
        else:
            parsed = None
        return SimpleNamespace(parsed=parsed, text=json.dumps(parsed))

    def complete(self, *, messages, **kw):
        self.calls.append(("text", messages[0]["content"].split("\n", 1)[0], kw.get("task")))
        if self.fail:
            raise RuntimeError("provider 500")
        return SimpleNamespace(text="• [0:52] They announced the price: $799.")


def default_judge(text: str) -> dict:
    import re
    for rid, line in re.findall(r"^\[(\d+)\] (.+)$", text, re.M):
        if re.search(r"dollars|seven ninety", line, re.I):
            return {"matched": True, "confidence": 0.92, "evidence_quote": line[:80],
                    "evidence_row_ids": [int(rid)], "reason": "price stated"}
    return {"matched": False, "confidence": 0.1, "evidence_quote": "", "evidence_row_ids": [], "reason": "no"}


class FakeCtx:
    def __init__(self, settings=None, llm=None):
        self.settings = {"stt_backend": "captions", **(settings or {})}
        self.llm = llm or FakeLLM()
        self.posts = []
        self.commands, self.tools, self.hooks, self.skills, self.cli, self.tasks = {}, {}, {}, {}, {}, {}
        self.unload = []
        self.lock = threading.Lock()

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def post_message(self, text):
        with self.lock:
            self.posts.append((time.time(), text))
        return SimpleNamespace(ok=True, error=None, message_id=str(len(self.posts)))

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = handler

    def register_tool(self, name, toolset, schema, handler, **kw):
        self.tools[name] = (schema, handler)

    def register_hook(self, name, cb):
        self.hooks[name] = cb

    def register_skill(self, name, path):
        self.skills[name] = path

    def register_cli_command(self, name, help, setup_fn, handler_fn):
        self.cli[name] = (setup_fn, handler_fn)

    def register_auxiliary_task(self, key, **kw):
        self.tasks[key] = kw

    def on_unload(self, cb):
        self.unload.append(cb)

    def texts(self):
        with self.lock:
            return [t for _, t in self.posts]


@pytest.fixture
def fake_ctx():
    return FakeCtx()
