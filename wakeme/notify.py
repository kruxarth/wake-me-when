"""Delivery chain: ctx.post_message → ctx.inject_message(session_key) → queue for /watching.

Measured on Hermes v0.23.0 (SPIKE.md 1e):
* messaging platforms: post_message sends a real message into the chat;
* TUI / Desktop: a notice; classic CLI: accepted but not displayed, so local alerts are also queued
  and shown on the next /watching (and offered to the agent through the pre_llm_call hook);
* posts never enter the conversation history and never carry MEDIA: attachments.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

LOCAL_PLATFORMS = {"local", "cli", "tui", "desktop", ""}


def post(ctx: Any, text: str) -> tuple[bool, str]:
    poster = getattr(ctx, "post_message", None)
    if poster is None:
        return False, "post_message unavailable"
    try:
        handle = poster(text)
    except Exception as exc:  # documented never to raise; defensive at the host boundary
        return False, str(exc)
    return bool(getattr(handle, "ok", False)), str(getattr(handle, "error", "") or "")


def deliver(ctx: Any, store: Any, watch: dict, text: str, *, alert: bool = False) -> str:
    """Send ``text`` to the chat that owns ``watch``. Returns the channel used."""
    ok, err = post(ctx, text)
    local = (watch.get("platform") or "").lower() in LOCAL_PLATFORMS
    if ok:
        if alert and local:
            store.queue_pending(watch["id"], watch.get("session_key") or "", text)
            return "posted+queued"
        return "posted"
    if alert and watch.get("session_key"):
        injector = getattr(ctx, "inject_message", None)
        if injector is not None:
            try:
                if injector(f"[Wake Me When — relay this alert to the user verbatim]\n{text}", role="user",
                            session_key=watch["session_key"]):
                    return "injected"
            except Exception:
                log.debug("wake-me-when: inject_message failed", exc_info=True)
    store.queue_pending(watch["id"], watch.get("session_key") or "", text)
    log.info("wake-me-when: watch #%s message queued for /watching (%s)", watch.get("id"), err)
    return "queued"
