"""Wake Me When — a Hermes plugin that watches live audio/video and pings you at the moment."""

from __future__ import annotations

import atexit
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_APP = None


def _register_tasks(ctx: Any) -> set[str]:
    """Own auxiliary slots so users can route the judge to a cheap model and vision separately."""
    reg = getattr(ctx, "register_auxiliary_task", None)
    if reg is None:
        return set()
    tasks = set()
    for key, kwargs in (
        ("wakeme_judge", dict(display_name="Wake Me When judge",
                              description="Decides whether a live stream's transcript shows the watched-for "
                                          "moment; also plans watches and writes catch-ups.",
                              defaults={"reasoning_effort": "low", "timeout": 60})),
        ("wakeme_vision", dict(display_name="Wake Me When vision",
                               description="Answers yes/no questions about live video frames.",
                               inherit_from="vision")),
    ):
        try:
            reg(key, **kwargs)
            tasks.add(key)
        except Exception:
            log.warning("wake-me-when: could not register auxiliary task %s", key, exc_info=True)
    return tasks


def register(ctx: Any) -> None:
    global _APP
    from .wakeme import commands, store as store_mod, tools
    from .wakeme.app import App

    store = store_mod.Store()
    stale = store.mark_orphans()
    if stale:
        log.info("wake-me-when: marked %d watch(es) stopped after a restart", stale)
    app = App(ctx, store, _register_tasks(ctx))
    _APP = app

    ctx.register_command("wakeme", commands.wakeme(app), description="Watch a live stream and wake me at the moment",
                         args_hint="<url> when <condition> | find | clip | notit | stop | doctor")
    ctx.register_command("watching", commands.watching(app), description="List Wake Me When watches")
    ctx.register_command("catchup", commands.catchup(app), description="Catch up on a watched stream",
                         args_hint="[id] [minutes]")
    for name, handler in tools.handlers(app).items():
        ctx.register_tool(name=name, toolset="wake_me_when", schema=tools.SCHEMAS[name], handler=handler)
    skill = Path(__file__).parent / "skills" / "wake-me-when" / "SKILL.md"
    if skill.exists():
        ctx.register_skill("wake-me-when", skill)
    ctx.register_hook("pre_llm_call", commands.pre_llm_hook(app))
    ctx.register_cli_command(name="wakeme", help="Wake Me When: doctor and status",
                             setup_fn=commands.cli_setup, handler_fn=commands.cli_handler(app))
    if hasattr(ctx, "register_automation_blueprint"):
        commands.register_blueprint(ctx)
    if hasattr(ctx, "on_unload"):
        ctx.on_unload(app.shutdown)
    from .wakeme.ingest import kill_all
    atexit.register(kill_all)  # last resort: never leave ffmpeg/streamlink behind
