"""Settings: one table drives the manifest's config_schema, defaults and type coercion."""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

# key: (type, default, description)
SCHEMA: dict[str, tuple[str, Any, str]] = {
    "whisper_model": ("str", "base", "Local speech model: tiny, base or small"),
    "language": ("str", "", "Speech language hint (e.g. en). Empty = auto-detect"),
    "stt_backend": ("str", "local", "Speech recognition backend: local (faster-whisper) or captions (demo/VOD captions only)"),
    "max_watches": ("int", 3, "Maximum live watches at once (all Hermes processes)"),
    "max_llm_calls_per_hour": ("int", 60, "LLM calls per watch per hour before falling back to free cue matching"),
    "semantic_check_sec": ("int", 60, "Seconds between periodic judge checks for paraphrases"),
    "min_confidence": ("float", 0.6, "Lowest judge confidence that can fire an alert"),
    "watch_ttl_hours": ("float", 6.0, "Watches stop automatically after this many hours"),
    "feedback_grace_min": ("float", 10.0, "Minutes a fired watch keeps buffering so clip / not it still work"),
    "clip_seconds": ("int", 20, "Length of a clip"),
    "auto_clip": ("bool", False, "Cut the clip as soon as an alert fires"),
    "auto_attach_clip": ("bool", True, "With allow_gateway_injection granted, ask the agent to attach the clip right after an alert (one agent turn)"),
    "catchup_summary_min": ("float", 10.0, "Minutes of speech per rolling catch-up summary"),
    "keep_transcripts_days": ("float", 7.0, "Days to keep transcripts and summaries"),
    "keep_clips_days": ("float", 2.0, "Days to keep clips you asked for"),
    "allow_private_network": ("bool", False, "Allow LAN / private addresses (e.g. a local RTSP camera)"),
    "allowed_media_dirs": ("list", [], "Directories whose files may be watched from messaging chats"),
}

_PY = {"str": str, "int": int, "float": float, "bool": bool, "list": list}


@dataclass
class Settings:
    whisper_model: str = "base"
    language: str = ""
    stt_backend: str = "local"
    max_watches: int = 3
    max_llm_calls_per_hour: int = 60
    semantic_check_sec: int = 60
    min_confidence: float = 0.6
    watch_ttl_hours: float = 6.0
    feedback_grace_min: float = 10.0
    clip_seconds: int = 20
    auto_clip: bool = False
    auto_attach_clip: bool = True
    catchup_summary_min: float = 10.0
    keep_transcripts_days: float = 7.0
    keep_clips_days: float = 2.0
    allow_private_network: bool = False
    allowed_media_dirs: list = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.allowed_media_dirs is None:
            self.allowed_media_dirs = []


def _coerce(kind: str, value: Any, default: Any) -> Any:
    if value is None:
        return default
    try:
        if kind == "bool":
            if isinstance(value, str):
                return value.strip().lower() in {"1", "true", "yes", "on"}
            return bool(value)
        if kind == "list":
            if isinstance(value, str):
                return [v.strip() for v in value.split(",") if v.strip()]
            return list(value)
        return _PY[kind](value)
    except (TypeError, ValueError):
        return default


def load(ctx: Any = None) -> Settings:
    """Read every setting through ``ctx.get_config`` (plugin-relative keys), falling back to defaults."""
    values = {}
    for f in fields(Settings):
        kind, default, _ = SCHEMA[f.name]
        raw = None
        if ctx is not None:
            try:
                raw = ctx.get_config(f.name, default=None)
            except Exception:
                raw = None
        values[f.name] = _coerce(kind, raw, default)
    s = Settings(**values)
    if s.whisper_model not in {"tiny", "base", "small"}:
        s.whisper_model = "base"
    s.max_watches = max(1, s.max_watches)
    s.clip_seconds = min(max(5, s.clip_seconds), 60)
    return s
