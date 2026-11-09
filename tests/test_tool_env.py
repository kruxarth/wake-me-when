"""External tools (ffmpeg, ffprobe, yt-dlp, streamlink, tesseract) get a minimal environment, never the
Hermes one with its provider keys and bot tokens."""

import ast
import json
import os
import sys
from pathlib import Path

import pytest

from wake_me_when.wakeme import ingest, sources
from wake_me_when.wakeme.sources import Source

ROOT = Path(__file__).resolve().parents[1]

SECRETS = {"OPENAI_API_KEY": "sk-test", "ANTHROPIC_API_KEY": "sk-ant-test", "OPENROUTER_API_KEY": "or-test",
           "TELEGRAM_BOT_TOKEN": "123:abc", "DISCORD_BOT_TOKEN": "d-test", "AWS_SECRET_ACCESS_KEY": "aws-test",
           "HERMES_API_KEY": "h-test", "PYTHONPATH": "/opt/hermes"}
KEPT = {"HOME": "/home/u", "TMPDIR": "/tmp/u", "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8",
        "XDG_CACHE_HOME": "/home/u/.cache", "HTTPS_PROXY": "http://proxy.test:3128", "no_proxy": "localhost",
        "SSL_CERT_FILE": "/etc/ssl/ca.pem", "TESSDATA_PREFIX": "/usr/share/tessdata"}


@pytest.fixture
def hermes_env(monkeypatch):
    for k, v in {**SECRETS, **KEPT}.items():
        monkeypatch.setenv(k, v)


def test_tool_env_is_an_allowlist(hermes_env):
    env = sources.tool_env()
    assert env["PATH"] == os.environ["PATH"]
    assert {k: env[k] for k in KEPT} == KEPT
    assert not set(SECRETS) & set(env)
    assert all(k in sources.TOOL_ENV_KEYS or k.startswith(sources.TOOL_ENV_PREFIXES) for k in env)


def _is_tool_env_call(node) -> bool:
    return isinstance(node, ast.Call) and "tool_env" in (getattr(node.func, "id", None), getattr(node.func, "attr", None))


def test_every_subprocess_call_passes_the_tool_env():
    calls = []
    for path in [ROOT / "__init__.py", *sorted((ROOT / "wakeme").glob("*.py"))]:   # everything the plugin runs
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        bound = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign) and _is_tool_env_call(n.value)
                 for t in n.targets if isinstance(t, ast.Name)}
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)):
                continue
            where = f"{path.name}:{n.lineno}"
            mod, fn = n.func.value.id, n.func.attr
            assert not (mod == "os" and fn.startswith(("system", "popen", "exec", "spawn", "posix_spawn"))), where
            if mod == "subprocess" and fn in {"run", "Popen", "call", "check_call", "check_output"}:
                env = next((k.value for k in n.keywords if k.arg == "env"), None)
                assert env is not None, f"{where} starts a tool without env="
                assert _is_tool_env_call(env) or (isinstance(env, ast.Name) and env.id in bound), where
                calls.append(where)
    assert len(calls) >= 11, calls   # the scan found the plugin's tool launches


def _fake_tool(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(f"#!{sys.executable}\nimport json, os\n{body}\n")
    path.chmod(0o755)
    return str(path)


def test_a_resolver_process_sees_only_the_tool_env(hermes_env, tmp_path, monkeypatch):
    fake = _fake_tool(tmp_path, "yt-dlp", "print(json.dumps(dict(os.environ)))")
    monkeypatch.setattr(sources, "need", lambda name: fake)
    seen = sources.ytdlp_info("https://youtu.be/x")   # the child's environment, read back as yt-dlp's JSON
    assert not set(SECRETS) & set(seen)
    assert seen["HOME"] == KEPT["HOME"] and seen["HTTPS_PROXY"] == KEPT["HTTPS_PROXY"]


def test_ingest_processes_see_only_the_tool_env(hermes_env, tmp_path, monkeypatch):
    out = tmp_path / "env"
    dump = "open({!r}, 'w').write(json.dumps(dict(os.environ)))"
    sl = _fake_tool(tmp_path, "streamlink", dump.format(f"{out}.streamlink"))
    ff = _fake_tool(tmp_path, "ffmpeg", dump.format(f"{out}.ffmpeg"))
    monkeypatch.setattr(sources, "need", lambda name: ff)
    src = Source(url="https://twitch.tv/x", kind="twitch", inputs=["pipe:0"], whitelist=sources.PIPE_WHITELIST,
                 pipe_argv=[sl, "-O", "--", "https://twitch.tv/x", "best"])
    ing = ingest.Ingest(src, tmp_path / "work", frames=False, ring=ingest.AudioRing())
    ing.start()
    try:
        ing.pipe.wait(15)   # both fakes exit on their own once they have written their environment
        ing.proc.wait(15)
    finally:
        ing.stop()
    for tool in ("streamlink", "ffmpeg"):
        env = json.loads(Path(f"{out}.{tool}").read_text())
        assert not set(SECRETS) & set(env), tool
        assert env["HOME"] == KEPT["HOME"], tool
