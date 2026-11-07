"""The /wakeme slash command, the pre_llm_call hook, the CLI command, the blueprint."""

from __future__ import annotations

import argparse
import logging
from typing import Any, Callable

from . import doctor
from .app import HELP, App, session_from_kwargs

log = logging.getLogger(__name__)

WRONG = {"wrong", "notit", "not-it", "nope"}  # documented: wrong; the rest are older spellings


def wakeme(app: App) -> Callable[..., str]:
    def handler(raw: str, **kw: Any) -> str:
        session = session_from_kwargs(kw)
        text = (raw or "").strip()
        if not text or text in {"help", "-h", "--help"}:
            return HELP
        sub, _, rest = text.partition(" ")
        sub = sub.lower()
        rest = rest.strip()
        if sub == "clip":
            path, msg = app.clip(rest)
            return f"{msg}\nMEDIA:{path}" if path else msg
        if sub == "not" and rest.lower().split(" ", 1)[0] == "it":  # "/wakeme not it"
            sub, rest = "wrong", rest[2:].strip()
        if sub in WRONG:
            target, _, note = rest.partition(" ")
            if target and not target.lstrip("#").isdigit():  # "/wakeme wrong that was a tease"
                target, note = "", rest
            return app.not_it(target, note.strip())
        if sub == "stop":
            return app.stop(rest)
        if sub == "find":
            return app.find(rest, session)
        if sub == "doctor":
            return doctor.run(app.settings().whisper_model, install=True, judge=doctor.judge_probe(app))
        if sub in {"list", "status", "watching"}:
            return app.watching(session)
        if sub == "catchup":
            return app.catchup(rest)
        return app.watch(text, session)
    return handler


def pre_llm_hook(app: App) -> Callable[..., Any]:
    def hook(session_id: str = "", **kw: Any) -> Any:
        try:
            key = kw.get("session_key") or session_from_kwargs({}).get("session_key", "")
            ctx_text = app.context_for(session_id=session_id or "", session_key=key)
        except Exception:
            log.debug("wake-me-when: context hook failed", exc_info=True)
            return None
        return {"context": ctx_text} if ctx_text else None
    return hook


# ---- `hermes wakeme ...` ---------------------------------------------------------------------

def cli_setup(sub: argparse.ArgumentParser) -> None:
    sp = sub.add_subparsers(dest="wakeme_command")
    d = sp.add_parser("doctor", help="Check dependencies, pre-warm speech recognition, run a 20 s demo watch")
    d.add_argument("--no-install", action="store_true", help="Don't install speech recognition")
    sp.add_parser("list", help="List watches")


def cli_handler(app: App) -> Callable[[argparse.Namespace], None]:
    def handler(args: argparse.Namespace) -> None:
        cmd = getattr(args, "wakeme_command", None)
        if cmd == "doctor":
            print(doctor.run(app.settings().whisper_model, install=not getattr(args, "no_install", False),
                             judge=doctor.judge_probe(app)))
        elif cmd == "list":
            print(app.watching())
        else:
            print("Usage: hermes wakeme {doctor|list}")
    return handler


def register_blueprint(ctx: Any) -> None:
    try:
        ctx.register_automation_blueprint(
            "watch-stream",
            title="Watch a stream for a moment",
            description="On a schedule, start watching a live stream and alert when something is said or shown.",
            category="general",
            schedule_template="{minute} {hour} * * {dow}",
            prompt_template=("Use the wakeme_watch tool to watch {url} and alert me when {condition}. "
                             "Reply with just the tool's result."),
            slots=[
                {"name": "url", "type": "text", "label": "Stream link", "default": "https://www.youtube.com/@NASA/live"},
                {"name": "condition", "type": "text", "label": "Wake me when…", "default": "they say liftoff"},
                {"name": "time", "type": "time", "label": "Start watching at", "default": "09:00"},
                {"name": "recurrence", "type": "weekdays", "label": "Repeat on", "default": "weekdays",
                 "options": ["everyday", "weekdays", "weekends"]},
            ],
            tags=["streams", "alerts"],
        )
    except Exception:
        log.warning("wake-me-when: automation blueprint not registered", exc_info=True)
