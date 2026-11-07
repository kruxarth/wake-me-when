"""Agent tool schemas and handlers (every handler returns a JSON string and never raises)."""

from __future__ import annotations

import json
from typing import Any, Callable

from . import plan as planmod
from .app import App, session_from_kwargs

_ID = {"type": "integer", "description": "Watch number, e.g. 3 (from the alert or wakeme_list)"}

SCHEMAS = {
    "wakeme_watch": {
        "name": "wakeme_watch",
        "description": ("Watch a LIVE audio/video stream (YouTube Live, Twitch, HLS .m3u8, RTSP camera, "
                        "demo://keynote) and alert the user in this chat the moment a spoken or on-screen "
                        "condition happens (e.g. 'they announce the price', 'T-minus 1 minute or a scrub'). "
                        "Returns immediately; the watch plan and alerts arrive as separate messages."),
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string", "description": "Stream link"},
            "condition": {"type": "string", "description": "What to wait for, in plain words"},
            "every_minutes": {"type": "number", "description": "Keep watching and alert every time, with this "
                              "cooldown in minutes. Omit to alert once."}},
            "required": ["url", "condition"]},
    },
    "wakeme_find": {
        "name": "wakeme_find",
        "description": ("Search a RECORDING (YouTube video, media link, local file, demo://keynote) for the "
                        "moments matching a question; returns ranked timestamps with evidence and deep links."),
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}, "question": {"type": "string"}}, "required": ["url", "question"]},
    },
    "wakeme_list": {
        "name": "wakeme_list",
        "description": "List the user's stream watches with their status, plus alerts they may have missed.",
        "parameters": {"type": "object", "properties": {}},
    },
    "wakeme_stop": {
        "name": "wakeme_stop",
        "description": "Stop a stream watch (or all of them).",
        "parameters": {"type": "object", "properties": {
            "watch_id": {"type": "string", "description": "Watch number, or 'all'"}}, "required": ["watch_id"]},
    },
    "wakeme_catchup": {
        "name": "wakeme_catchup",
        "description": ("Summarise, with stream timestamps, what happened on a watched stream: since the last "
                        "alert, since it started, or over the last N minutes."),
        "parameters": {"type": "object", "properties": {
            "watch_id": _ID, "minutes": {"type": "number", "description": "Only the last N minutes"}}},
    },
    "wakeme_clip": {
        "name": "wakeme_clip",
        "description": ("Get a short video clip of the moment a watch alerted on (or of the last 20 seconds if it "
                        "hasn't alerted). The clip is attached to your reply automatically."),
        "parameters": {"type": "object", "properties": {"watch_id": _ID}, "required": ["watch_id"]},
    },
    "wakeme_feedback": {
        "name": "wakeme_feedback",
        "description": ("Tell a watch its last alert was wrong ('not it'). It resumes watching and treats that "
                        "moment as a negative example."),
        "parameters": {"type": "object", "properties": {
            "watch_id": _ID, "verdict": {"type": "string", "enum": ["not_it"]},
            "note": {"type": "string", "description": "Optional: why it wasn't it"}},
            "required": ["watch_id"]},
    },
}


def _safe(fn: Callable[..., Any]) -> Callable[..., str]:
    def handler(args: dict, **kwargs: Any) -> str:
        try:
            return fn(args or {}, **kwargs)
        except Exception as exc:  # tool contract: never raise
            return json.dumps({"error": f"{exc.__class__.__name__}: {exc}"[:300]})
    return handler


def handlers(app: App) -> dict[str, Callable[..., str]]:
    def watch(args: dict, **kw: Any) -> str:
        every = args.get("every_minutes")
        req = planmod.WatchRequest(url=str(args.get("url", "")).strip(), condition=str(args.get("condition", "")).strip(),
                                   mode="every" if every else "once", cooldown_min=float(every or 5))
        if not req.url or len(req.condition) < 3:
            return json.dumps({"error": "url and condition are required"})
        return json.dumps({"result": app.start_watch(req, session_from_kwargs(kw))})

    def find(args: dict, **kw: Any) -> str:
        raw = f"{args.get('url', '')} {args.get('question', '')}"
        return json.dumps({"result": app.find(raw, session_from_kwargs(kw))})

    def lst(args: dict, **kw: Any) -> str:
        return json.dumps({"result": app.watching(session_from_kwargs(kw))})

    def stop(args: dict, **kw: Any) -> str:
        return json.dumps({"result": app.stop(str(args.get("watch_id", "")))})

    def catchup(args: dict, **kw: Any) -> str:
        minutes = args.get("minutes")
        return json.dumps({"result": app.catchup_for(str(args.get("watch_id") or ""),
                                                     float(minutes) if minutes else None)})

    def clip(args: dict, **kw: Any) -> str:
        path, msg = app.clip(str(args.get("watch_id", "")))
        if path is None:
            return json.dumps({"error": msg})
        return json.dumps({"result": msg, "clip": str(path)}) + f"\nMEDIA:{path}"

    def feedback(args: dict, **kw: Any) -> str:
        return json.dumps({"result": app.not_it(str(args.get("watch_id", "")), str(args.get("note") or ""))})

    table = {"wakeme_watch": watch, "wakeme_find": find, "wakeme_list": lst, "wakeme_stop": stop,
             "wakeme_catchup": catchup, "wakeme_clip": clip, "wakeme_feedback": feedback}
    return {name: _safe(fn) for name, fn in table.items()}
