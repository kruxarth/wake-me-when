"""Command and tool implementations shared by the slash commands and the agent tools."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

from . import asr, config, notify, plan as planmod, sources, vod
from .store import OWNER_TOKEN, Store, owner_alive
from .watcher import Manager

log = logging.getLogger(__name__)

HELP = """Wake Me When — watches live streams and pings you at the moment.
/wakeme <url> when <condition>     watch (say "every time …" for repeats)
/wakeme <recording url> <question> find the moment in a recording
/wakeme list · /wakeme catchup [id] · /wakeme clip [id] · /wakeme stop [id|all]
After an alert, just reply: clip · not it · stop      (/wakeme doctor checks setup)
Try: /wakeme demo://keynote when they announce the price"""


def session_from_kwargs(kw: dict) -> dict:
    """The calling chat, from slash-command keywords or (for tools) the bound session env."""
    sess = {"session_key": kw.get("session_key") or "", "session_id": kw.get("session_id") or "",
            "platform": (kw.get("platform") or "").lower()}
    if not (sess["session_key"] and sess["platform"]):
        try:
            from gateway.session_context import get_session_env
        except ImportError:
            get_session_env = None
        if get_session_env is not None:
            sess["session_key"] = sess["session_key"] or get_session_env("HERMES_SESSION_KEY", "")
            sess["session_id"] = sess["session_id"] or get_session_env("HERMES_SESSION_ID", "")
            sess["platform"] = sess["platform"] or (get_session_env("HERMES_SESSION_PLATFORM", "")
                                                    or get_session_env("HERMES_SESSION_SOURCE", "")).lower()
    return sess


def _ago(ts: Optional[float]) -> str:
    if not ts:
        return "?"
    s = int(time.time() - ts)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


class App:
    def __init__(self, ctx: Any, store: Optional[Store] = None, tasks: Optional[set[str]] = None):
        self.ctx = ctx
        self.store = store or Store()
        self.tasks = tasks or set()
        self.mgr = Manager(ctx, self.store, self.settings, self.tasks)

    def settings(self) -> config.Settings:
        return config.load(self.ctx)

    # ---- watch ---------------------------------------------------------------------------------

    def watch(self, raw: str, session: dict) -> str:
        """``/wakeme <url> <text>``: "when …" watches; a question about a recording searches it (find);
        a question about a live stream, or unclear wording, watches."""
        try:
            req = planmod.parse_watch(raw)
        except ValueError as exc:
            return str(exc)
        if req.phrasing == "search":
            s = self.settings()
            try:
                kind = sources.check_url(req.url, surface=session.get("platform", ""),
                                         allow_private=s.allow_private_network, allowed_dirs=s.allowed_media_dirs)
                recording = sources.is_recording(req.url, kind)
            except sources.SourceError as exc:
                return f"❌ {exc}"
            if recording:
                return self.find(raw, session)
        reply = self.start_watch(req, session)
        if req.phrasing == "unclear" and reply.startswith("👀") and sources.looks_recorded(req.url):
            reply += "\nIf this is a recording, /wakeme find <url> <question> searches it instead."
        return reply

    def start_watch(self, req: planmod.WatchRequest, session: dict) -> str:
        s = self.settings()
        try:
            kind = sources.check_url(req.url, surface=session.get("platform", ""), network=False,
                                     allow_private=s.allow_private_network, allowed_dirs=s.allowed_media_dirs)
        except sources.SourceError as exc:
            return f"❌ {exc}"
        try:
            w = self.mgr.create(req, kind, session)
        except RuntimeError as exc:
            return f"❌ {exc}"
        every = f" (every time, {req.cooldown_min:g} min cooldown)" if req.mode == "every" else ""
        lines = [f"👀 Watch #{w.id} started · planning…", f"Condition: {req.condition}{every}",
                 f"/wakeme list for status · /wakeme stop {w.id}"]
        if session.get("platform") in notify.LOCAL_PLATFORMS:
            lines.append("It runs in your Hermes gateway and keeps going if you close this window; "
                         "alerts that can't reach you wait in /wakeme list.")
        return "\n".join(lines)

    # ---- list / stop --------------------------------------------------------------------------

    def watching(self, session: Optional[dict] = None) -> str:
        live = self.store.watches(live_only=True)
        recent = [w for w in self.store.watches(limit=6) if w["status"] not in ("planning", "watching", "behind", "fired")]
        out = [f"👀 Watching ({len(live)})" if live else "Nothing is being watched. /wakeme <url> when <condition>"]
        for row in live:
            w = self.mgr.get(row["id"])
            status = w.status if w else row["status"]
            calls = f" · {w.calls_last_hour()} model calls/h" if w else ""
            where = "" if w or row.get("owner") == OWNER_TOKEN else \
                (" · running in another Hermes process" if owner_alive(row.get("owner") or "") else " · owner gone")
            out.append(f"#{row['id']} · {row.get('title') or row['url']}\n"
                       f"   “{row['condition']}” · {status} · up {_ago(row['created'])} · "
                       f"{row.get('alerts') or 0} alert(s){calls}{where}")
        if recent:
            out.append("Recent: " + " · ".join(f"#{r['id']} {r['status']}"
                                               + (f" ({r['note']})" if r.get("note") else "") for r in recent[:4]))
        pending = self.store.take_pending((session or {}).get("session_key", "") or "")
        if pending:
            out.append("Alerts you may have missed:")
            out += [f"[{time.strftime('%H:%M', time.localtime(p[2]))}] {p[3]}" for p in pending[-5:]]
        return "\n".join(out)

    def stop(self, target: str) -> str:
        target = (target or "").strip().lstrip("#")
        if target == "all":
            ids = self.mgr.stop_all()
            return f"⏹ Stopped {len(ids)} watch(es)." if ids else "Nothing to stop."
        if not target:
            w, msg = self._watch_or_msg("")
            return self.mgr.stop(w.id) if w else msg
        if not target.isdigit():
            return "Usage: /wakeme stop [id|all]"
        return self.mgr.stop(int(target))

    # ---- feedback / clip / catch-up -------------------------------------------------------------

    def _watch_or_msg(self, target: str) -> tuple[Optional[Any], str]:
        target = (target or "").strip().lstrip("#")
        if not target:
            live = list(self.mgr.watches.values())
            if len(live) == 1:
                return live[0], ""
            if not live:
                return None, "No watch is running in this Hermes. /wakeme list shows them."
            return None, "Which watch? " + ", ".join(f"#{w.id} ({w.req.condition})" for w in live)
        if not target.isdigit():
            return None, "Give the watch number, e.g. /wakeme clip 3"
        w = self.mgr.get(int(target))
        if w is None:
            row = self.store.watch(int(target))
            if row is None:
                return None, f"There's no watch #{target}."
            return None, f"Watch #{target} is {row['status']}" + (f" ({row['note']})" if row.get("note") else "") + "."
        return w, ""

    def not_it(self, target: str, note: str = "") -> str:
        w, msg = self._watch_or_msg(target)
        return w.not_it(note) if w else msg

    def clip(self, target: str) -> tuple[Optional[Path], str]:
        w, msg = self._watch_or_msg(target)
        if w is not None:
            return w.clip()
        t = (target or "").strip().lstrip("#")
        if t.isdigit() and self.store.watch(int(t)):
            return self.mgr.evidence_clip(int(t))
        return None, msg

    def catchup(self, raw: str) -> str:
        """``/wakeme catchup [id] [minutes]``. A lone number is a watch id when such a watch runs, else minutes."""
        nums = [p.lstrip("#") for p in (raw or "").split()]
        nums = [p[:-1] if p.endswith("m") else p for p in nums]
        nums = [p for p in nums if re.fullmatch(r"\d+(?:\.\d+)?", p)]
        target, minutes = "", None
        if len(nums) >= 2:
            target, minutes = nums[0], float(nums[1])
        elif len(nums) == 1:
            if nums[0].isdigit() and self.mgr.get(int(nums[0])):
                target = nums[0]
            else:
                minutes = float(nums[0])
        return self.catchup_for(target, minutes)

    def catchup_for(self, target: str, minutes: Optional[float] = None) -> str:
        w, msg = self._watch_or_msg(target)
        return w.catchup(minutes) if w else msg

    # ---- find ----------------------------------------------------------------------------------

    def find(self, raw: str, session: dict) -> str:
        tokens = (raw or "").split()
        url_idx = next((i for i, t in enumerate(tokens) if planmod._URLISH.match(t)), None)
        if url_idx is None:
            return "Usage: /wakeme find <url> <question>"
        url = tokens.pop(url_idx).strip("<>")
        question = " ".join(tokens).strip(" ?") or ""
        if len(question) < 3:
            return "What should I look for? Usage: /wakeme find <url> <question>"
        s = self.settings()

        def rerank(instructions: str, text: str, schema: dict) -> Any:
            return self.mgr.llm.structured(instructions, text, schema, task="wakeme_judge", purpose="wakeme.find",
                                           max_tokens=700, timeout=90)

        return vod.find(url, question, surface=session.get("platform", ""), allow_private=s.allow_private_network,
                        allowed_dirs=s.allowed_media_dirs, language=s.language or None,
                        rerank=rerank if getattr(self.ctx, "llm", None) is not None else None)

    # ---- agent context -------------------------------------------------------------------------

    def context_for(self, session_id: str = "", session_key: str = "") -> Optional[str]:
        """pre_llm_call: tell the agent which watches belong to this chat, so a plain "clip" or
        "not it" can be mapped to a watch id (posted alerts never enter the conversation history)."""
        mine = []
        for w in list(self.mgr.watches.values()):
            if (session_id and w.session.get("session_id") == session_id) or \
               (session_key and w.session.get("session_key") == session_key):
                mine.append(w)
        if not mine:
            return None
        lines = ["[Wake Me When plugin] Watches in this chat (alerts were posted outside the conversation):"]
        for w in mine:
            a = w.alerts[-1] if w.alerts else None
            state = w.status + (f", last alert {_ago(a['fired'])} ago: “{a['quote'][:120]}”" if a else "")
            lines.append(f"- #{w.id}: “{w.req.condition}” — {state}")
        alerted = [w for w in mine if w.alerts]
        last = max(alerted, key=lambda w: w.alerts[-1]["fired"]) if alerted else None
        if last is not None:
            lines.append(f"Watch #{last.id} alerted most recently. Each alert ends with “Reply clip, not it or "
                         f"stop”, so a short reply right after one is about it: “clip” / “show me” → wakeme_clip; "
                         f"“not it” / “wrong” / “nope” → wakeme_feedback (it keeps watching); “stop” → "
                         f"wakeme_stop; “catch me up” → wakeme_catchup. Use watch_id {last.id} unless the user "
                         "names another watch.")
        else:
            lines.append("If the user says clip / stop / catch me up without a number, use the matching wakeme_* "
                         "tool with that watch_id; if more than one watch fits, ask which.")
        return "\n".join(lines)

    # ---- shutdown -------------------------------------------------------------------------------

    def shutdown(self) -> None:
        from .ingest import kill_all
        try:
            self.mgr.stop_all(reason="plugin unloaded", wait=5)
        finally:
            kill_all()
            asr.WORKER.stop()
