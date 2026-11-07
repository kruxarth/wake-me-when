"""Per-watch thread wiring it all together, and the process-wide manager."""

from __future__ import annotations

import collections
import concurrent.futures
import contextvars
import logging
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import asr, clips, cues, notify, plan as planmod, sources, summary, vision
from .config import Settings
from .ingest import AudioRing, Ingest
from .judge import JUDGE_INSTRUCTIONS, JUDGE_SCHEMA, Debouncer, build_judge_input, map_evidence, parse_verdict
from .store import Store, data_dir, owner_alive, OWNER_TOKEN

log = logging.getLogger(__name__)

JUDGE_LOOKBACK_S = 150
HIGH_CONF = 0.85


def trim_overlap(prev: str, text: str, max_words: int = 12) -> str:
    """Drop the words at the start of ``text`` that repeat the end of ``prev`` (overlapping windows
    can transcribe the same 2 s twice with different segment boundaries)."""
    pw, tw = cues.normalize(prev).split(), text.split()
    nt = [cues.normalize(w) for w in tw]
    for k in range(min(max_words, len(pw), len(tw)), 1, -1):
        if pw[-k:] == [w for w in nt[:k] if w] and all(nt[:k]):
            return " ".join(tw[k:]).strip()
    return text


class BudgetExceeded(Exception):
    pass


class LLM:
    """``ctx.llm`` with per-watch call accounting and aux-task routing (when the host supports it)."""

    def __init__(self, ctx: Any, tasks: set[str]):
        self.ctx = ctx
        self.tasks = tasks

    def _task(self, task: Optional[str]) -> dict:
        return {"task": task} if task and task in self.tasks else {}

    def structured(self, instructions: str, text: str, schema: dict, *, task: Optional[str], purpose: str,
                   max_tokens: int = 500, timeout: float = 90, images: Optional[list[bytes]] = None) -> Any:
        payload: list[dict] = [{"type": "text", "text": text}]
        for img in images or []:
            payload.append({"type": "image", "data": img, "mime_type": "image/jpeg"})
        res = self.ctx.llm.complete_structured(instructions=instructions, input=payload, json_schema=schema,
                                               purpose=purpose, temperature=0.0, max_tokens=max_tokens,
                                               timeout=timeout, **self._task(task))
        if getattr(res, "parsed", None) is not None:
            return res.parsed
        import json
        text_out = (getattr(res, "text", "") or "").strip().strip("`")
        if text_out.startswith("json"):
            text_out = text_out[4:]
        try:
            return json.loads(text_out[text_out.find("{"): text_out.rfind("}") + 1])
        except ValueError:
            return None

    def text(self, instructions: str, text: str, *, task: Optional[str], purpose: str, max_tokens: int = 600,
             timeout: float = 90) -> str:
        res = self.ctx.llm.complete(messages=[{"role": "system", "content": instructions},
                                              {"role": "user", "content": text}],
                                    max_tokens=max_tokens, temperature=0.2, timeout=timeout, purpose=purpose,
                                    **self._task(task))
        return (getattr(res, "text", "") or "").strip()


class Watch:
    def __init__(self, mgr: "Manager", wid: int, req: planmod.WatchRequest, kind: str, session: dict):
        self.mgr = mgr
        self.ctx = mgr.ctx
        self.store: Store = mgr.store
        self.s: Settings = mgr.settings_fn()
        self.id = wid
        self.req = req
        self.kind = kind
        self.session = session
        self.src: Optional[sources.Source] = None
        self.plan: Optional[planmod.Plan] = None
        self.deb: Optional[Debouncer] = None
        self.workdir = data_dir() / "watches" / f"w{wid}"
        self.ring = AudioRing()
        self.ingest: Optional[Ingest] = None
        self.stop_event = threading.Event()
        self.stop_reason = ""
        self.status = "planning"
        self.started = time.time()
        self.inbox: collections.deque = collections.deque()
        self.next_win = 0.0
        self.first_win = True
        self.use_captions = False
        self.llm_calls: collections.deque = collections.deque()
        self.budget_warned = False
        self.judge_pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix=f"wakeme-judge-{wid}")
        self.side_pool = concurrent.futures.ThreadPoolExecutor(1, thread_name_prefix=f"wakeme-side-{wid}")
        self.judge_future: Optional[concurrent.futures.Future] = None
        self.last_judge = 0.0
        self.last_judged_row = 0
        self.pending_since_row: Optional[int] = None
        self.stage_a_last: Optional[cues.StageAResult] = None
        self.negatives: list[str] = []
        self.ocr_text = ""
        self.frame_gate = vision.FrameGate()
        self.last_ocr = 0.0
        self.last_vision = 0.0
        self.alerts: list[dict] = []
        self.fired_at: Optional[float] = None
        self.cooldown_until = 0.0
        self.sum_end = 0.0
        self.sum_future: Optional[concurrent.futures.Future] = None
        self.restarts: collections.deque = collections.deque()
        self.lock = threading.RLock()
        self.thread: Optional[threading.Thread] = None
        self.ended = threading.Event()
        self.judge_latencies: list[float] = []
        self.trigger_judge = False
        self.last_text, self.last_end = "", -1e9
        self.periodic_s = float(self.s.semantic_check_sec)
        self.ocr_at = 0.0

    # ---- helpers ------------------------------------------------------------------------------

    @property
    def row(self) -> dict:
        return {"id": self.id, "platform": self.session.get("platform", ""),
                "session_key": self.session.get("session_key", "")}

    @property
    def pos(self) -> float:
        return self.ring.end_pos

    def say(self, text: str, alert: bool = False) -> str:
        return notify.deliver(self.ctx, self.store, self.row, text, alert=alert)

    def set_status(self, status: str, note: str = "") -> None:
        if status != self.status or note:
            self.status = status
            fields = {"status": status}
            if note:
                fields["note"] = note
            self.store.update_watch(self.id, **fields)

    def calls_last_hour(self) -> int:
        cutoff = time.time() - 3600
        while self.llm_calls and self.llm_calls[0] < cutoff:
            self.llm_calls.popleft()
        return len(self.llm_calls)

    def _spend(self) -> None:
        if self.calls_last_hour() >= self.s.max_llm_calls_per_hour:
            if not self.budget_warned:
                self.budget_warned = True
                self.say(f"⚠️ Watch #{self.id} used its {self.s.max_llm_calls_per_hour} model checks for this "
                         "hour. Until that resets I only alert on strong cue matches, marked unconfirmed.")
            raise BudgetExceeded()
        self.llm_calls.append(time.time())

    def structured(self, instructions: str, text: str, schema: dict, task: str, purpose: str, **kw: Any) -> Any:
        self._spend()
        return self.mgr.llm.structured(instructions, text, schema, task=task, purpose=purpose, **kw)

    def complete(self, instructions: str, text: str, task: str, purpose: str, **kw: Any) -> str:
        self._spend()
        return self.mgr.llm.text(instructions, text, task=task, purpose=purpose, **kw)

    # ---- lifecycle ----------------------------------------------------------------------------

    def start(self) -> None:
        """Run on a thread that inherits the caller's context, so post_message keeps the chat binding."""
        ctx = contextvars.copy_context()
        self.thread = threading.Thread(target=ctx.run, args=(self._main,), name=f"wakeme-watch-{self.id}",
                                       daemon=True)
        self.thread.start()

    def request_stop(self, reason: str) -> None:
        self.stop_reason = self.stop_reason or reason
        self.stop_event.set()

    def _main(self) -> None:
        final_status, note, message = "ended", "", None
        try:
            final_status, note, message = self._run()
        except Exception as exc:
            log.warning("wake-me-when: watch #%s crashed", self.id, exc_info=True)
            final_status, note, message = "ended", f"error: {exc}"[:200], f"❌ Watch #{self.id} stopped: {exc}"[:400]
        finally:
            self._cleanup(final_status, note)
            if message:
                self.say(message)
            self.ended.set()

    def _run(self) -> tuple[str, str, Optional[str]]:
        try:
            sources.check_url(self.req.url, surface=self.session.get("platform", ""),
                              allow_private=self.s.allow_private_network, allowed_dirs=self.s.allowed_media_dirs)
            self.src = sources.resolve(self.req.url, self.kind)
        except sources.SourceError as exc:
            return "ended", f"source: {exc}", f"❌ Watch #{self.id} couldn't open the stream: {exc}"
        self.store.update_watch(self.id, title=self.src.title)
        if self.stop_event.is_set():
            return "stopped", self.stop_reason, None
        self.plan, err = self._make_plan()
        if self.plan is None:
            return "ended", f"plan: {err}", f"❌ Watch #{self.id} couldn't plan the watch: {err}"
        self.store.update_watch(self.id, plan=self.plan.to_dict())
        if not self.req.mode_set and self.plan.mode:  # the model read "each time …" the regex didn't catch
            self.req.mode = self.plan.mode
            if self.plan.cooldown_min:
                self.req.cooldown_min = self.plan.cooldown_min
            self.store.update_watch(self.id, mode=self.req.mode)
        self.deb = Debouncer(min_conf=max(self.s.min_confidence, self.plan.min_confidence), confirm=self.plan.confirm)
        asr.WORKER.language = self.s.language or None

        extra = []
        if self.s.stt_backend == "captions" or (self.src.captions and not asr.ready()):
            if not self.src.captions:
                return "ended", "no captions", (f"❌ Watch #{self.id}: `stt_backend` is captions but this source has "
                                                "no captions.")
            self.use_captions = True
            if self.src.kind != "demo":
                extra.append("Using the file's captions for speech.")
            if self.s.stt_backend != "captions":
                self.mgr.warm_asr()
        elif not asr.ready():
            extra.append("Installing/loading speech recognition first (about 1–3 minutes the first time).")
            self.mgr.warm_asr()
        if self.session.get("platform") in notify.LOCAL_PLATFORMS:
            extra.append("This watch runs in your Hermes gateway; alerts also show in /wakeme list.")
        frames = self.plan.needs_frames and bool(sources.which("tesseract") or self.plan.visual_question)
        if self.plan.ocr_patterns and not sources.which("tesseract"):
            extra.append("On-screen text needs tesseract (not installed), so I'm listening only.")
        if self.req.mode == "every":
            extra.append(f"Alerting every time, at most once every {self.req.cooldown_min:g} min.")
        by = f" · {self.src.channel}" if self.src.channel and self.src.channel != "Wake Me When" else ""
        self.say(f"📋 Watch #{self.id} · {self.src.title}{by} ({self.src.label})\n{planmod.describe(self.plan)}\n"
                 + ("\n".join(extra) + "\n" if extra else "") + "I'll ping you here.")

        self.workdir.mkdir(parents=True, exist_ok=True)
        self.ingest = Ingest(self.src, self.workdir, frames, self.ring)
        self.ingest.start()
        self.set_status("watching")
        if not self.use_captions:
            asr.WORKER.start()
        return self._loop()

    def _make_plan(self) -> tuple[Optional[planmod.Plan], str]:
        err = ""
        for attempt in range(2):
            try:
                p = planmod.make_plan(lambda i, t, sch: self.structured(i, t, sch, "wakeme_judge", "wakeme.plan",
                                                                        max_tokens=600),
                                      self.req.condition)
                return p, ""
            except BudgetExceeded:
                return None, "the LLM budget is used up"
            except Exception as exc:
                err = str(exc)[:200] or exc.__class__.__name__
                log.info("wake-me-when: plan attempt %d failed: %s", attempt + 1, err)
                if self.stop_event.wait(3):
                    break
        return None, f"the model call failed ({err})"

    # ---- main loop ----------------------------------------------------------------------------

    def _loop(self) -> tuple[str, str, Optional[str]]:
        ttl = self.s.watch_ttl_hours * 3600
        last_sum_check = time.time()
        while not self.stop_event.wait(0.4):
            now = time.time()
            if now - self.started > ttl:
                return "ended", "expired", (f"⏱ Watch #{self.id} expired after {self.s.watch_ttl_hours:g} h "
                                            f"without the moment. /wakeme {self.req.url} when {self.req.condition} "
                                            "to watch again.")
            self._produce_windows(final=False)
            self._ingest_rows()
            self._collect_judge()
            if self.trigger_judge and self._maybe_judge(trigger=True) is not None:
                self.trigger_judge = False
            self._maybe_judge()
            self._maybe_frames()
            if now - last_sum_check > 20:
                last_sum_check = now
                self._maybe_summarize()
            self.ingest.prune()
            self._update_behind()
            if self.status == "fired" and self.fired_at and now - self.fired_at > self.s.feedback_grace_min * 60:
                return "ended", "done", None
            if self.ingest.eof.is_set() and not self.ingest.alive():
                outcome = self._on_eof()
                if outcome is not None:
                    return outcome
        return "stopped", self.stop_reason or "stopped", None

    def _on_eof(self) -> Optional[tuple[str, str, Optional[str]]]:
        finite = self.src.kind in {"demo", "file"} or not self.src.is_live
        if finite:
            self._produce_windows(final=True)
            deadline = time.time() + 120
            while time.time() < deadline and not self.stop_event.is_set():
                self._ingest_rows()
                self._collect_judge()
                busy = asr.WORKER.backlog(self.id) or self.inbox or (self.judge_future and not self.judge_future.done())
                if not busy and self._maybe_judge(force_tail=True) is False:
                    break
                time.sleep(0.3)
            self._collect_judge()
            if self.status == "fired":
                self._wait_grace()
            if self.stop_event.is_set():  # /wakeme stop during the drain or grace window
                return "stopped", self.stop_reason or "stopped", None
            return "ended", "stream ended", (f"🏁 Watch #{self.id}: the stream ended"
                                             + ("." if self.alerts else " without the moment."))
        tail = self.ingest.stderr_tail if self.ingest else ""
        now = time.time()
        self.restarts.append(now)
        while self.restarts and now - self.restarts[0] > 600:
            self.restarts.popleft()
        if len(self.restarts) > 5:
            return "ended", "stream ended", f"🏁 Watch #{self.id}: the stream ended or keeps failing. {tail[-160:]}"
        self.ingest.stop()
        delay = min(30, 2 ** len(self.restarts))
        log.info("wake-me-when: watch #%s ingest stopped, restarting in %ss", self.id, delay)
        if self.stop_event.wait(delay):
            return None
        try:
            fresh = sources.resolve(self.req.url, self.kind)   # stream URLs expire; re-resolve
            self.src.inputs, self.src.pipe_argv = fresh.inputs, fresh.pipe_argv
        except sources.SourceError as exc:
            return "ended", "stream ended", f"🏁 Watch #{self.id}: the stream ended ({exc})."
        self.ingest.start(base_pos=self.ring.end_pos)
        return None

    def _wait_grace(self) -> None:
        """Finite stream ended while fired: keep the evidence reachable until the grace window closes."""
        while self.status == "fired" and self.fired_at and not self.stop_event.is_set() \
                and time.time() - self.fired_at < self.s.feedback_grace_min * 60:
            self.stop_event.wait(1)

    def _produce_windows(self, final: bool) -> None:
        while True:
            end = self.next_win + asr.WINDOW_S
            if self.ring.end_pos < end and not final:
                return
            if final and self.ring.end_pos <= self.next_win + 0.2:
                return
            start, pcm = self.ring.read(self.next_win, min(end, self.ring.end_pos))
            if start > self.next_win + 0.5:  # audio dropped before we got to it
                self.set_status("behind")
            pos = self.next_win
            is_final = final and self.ring.end_pos <= end
            if self.use_captions:
                segs = asr.captions_for(self.src.captions, pos, self.first_win, is_final)
                self.inbox.extend(segs)
            else:
                asr.WORKER.submit(asr.Job(self.id, start, pcm, self.first_win, is_final, self.inbox.extend))
            self.first_win = False
            self.next_win += asr.WINDOW_S - asr.OVERLAP_S
            if is_final:
                return

    def _ingest_rows(self) -> None:
        new: list[tuple[int, str]] = []
        while self.inbox:
            seg = self.inbox.popleft()
            text = trim_overlap(self.last_text, seg.text) if seg.start - self.last_end < asr.OVERLAP_S + 1 else seg.text
            if not text:
                continue
            wall = self.ring.wall_at(seg.start) or time.time()
            rid = self.store.add_row(self.id, seg.start, seg.end, wall, text)
            self.last_text, self.last_end = text, seg.end
            new.append((rid, text))
        if not new or self.status == "fired" or self.plan is None:
            return
        # speech only: on-screen text triggers from _maybe_frames when a NEW frame matches
        res = cues.match(new, self.plan.cue_phrases, self.plan.negative_cues)
        self.stage_a_last = res if res.hit else self.stage_a_last
        if res.hit:
            self.periodic_s = float(self.s.semantic_check_sec)
            self._maybe_judge(trigger=True)

    def _judge_rows(self) -> list[tuple[int, str]]:
        last_fire_pos = self.alerts[-1]["pos"] if self.alerts else 0.0
        since = max(self.pos - JUDGE_LOOKBACK_S, last_fire_pos, 0.0)
        rows = self.store.rows(self.id, since_pos=since, after_id=self.deb.last_fire_row if self.deb else 0)
        return [(r[0], r[4]) for r in rows][-40:]

    def _maybe_judge(self, trigger: bool = False, force_tail: bool = False) -> Optional[bool]:
        """Start a judge call when warranted. Returns True if one started, False if nothing to judge."""
        if self.deb is None or self.status == "fired" or time.time() < self.cooldown_until:
            return False
        if self.judge_future is not None and not self.judge_future.done():
            return None
        rows = self._judge_rows()
        if not rows or rows[-1][0] <= self.last_judged_row and not trigger:
            return False
        new_speech = rows[-1][0] > self.last_judged_row
        due = (time.time() - self.last_judge >= self.periodic_s) and new_speech
        follow_up = self.pending_since_row is not None and new_speech
        if not (trigger or due or follow_up or (force_tail and new_speech)):
            return False
        ocr = self.ocr_text if time.time() - self.ocr_at < 20 else ""
        stage_a = cues.match(rows, self.plan.cue_phrases, self.plan.negative_cues, ocr, self.plan.ocr_patterns)
        self.last_judge = time.time()
        self.last_judged_row = rows[-1][0]
        self.judge_future = self.judge_pool.submit(self._judge_call, rows, stage_a, ocr)
        return True

    def _judge_call(self, rows: list[tuple[int, str]], stage_a: cues.StageAResult, ocr: str = "") -> dict:
        text = build_judge_input(self.req.condition, rows, ocr, self.negatives)
        t = time.time()
        try:
            parsed = self.structured(JUDGE_INSTRUCTIONS, text, JUDGE_SCHEMA, "wakeme_judge", "wakeme.judge",
                                     max_tokens=400, timeout=60)
            ok = isinstance(parsed, dict)
            err = "" if ok else "unparseable verdict"
        except BudgetExceeded:
            parsed, ok, err = None, False, "budget"
        except Exception as exc:
            parsed, ok, err = None, False, str(exc)[:200] or exc.__class__.__name__
        return {"rows": rows, "parsed": parsed, "ok": ok, "err": err, "stage_a": stage_a, "latency": time.time() - t}

    def _collect_judge(self) -> None:
        f = self.judge_future
        if f is None or not f.done():
            return
        self.judge_future = None
        try:
            res = f.result()
        except Exception:
            return
        with self.lock:
            self._apply_verdict(res)

    def _apply_verdict(self, res: dict) -> None:
        if self.status == "fired" or self.deb is None:
            return
        rows, stage_a = res["rows"], res["stage_a"]
        if not res["ok"]:
            self.store.event(self.id, "judge_error", self.pos, error=res["err"])
            d = self.deb.stage_a_only(stage_a.strong, stage_a.row_ids)
            if d.action == "fire":
                self._fire(d, rows)
            return
        self.judge_latencies.append(res["latency"])
        matched, conf, quote = parse_verdict(res["parsed"])
        ids = map_evidence(res["parsed"], rows) if matched else []
        d = self.deb.judge(matched, conf, ids, quote, newest_row=rows[-1][0] if rows else 0)
        self.store.event(self.id, "verdict", self.pos, matched=matched, confidence=conf, rows=ids,
                         action=d.action, latency=round(res["latency"], 2))
        if d.action == "fire":
            self.pending_since_row = None
            self._fire(d, rows)
        elif d.action == "pending":
            self.pending_since_row = max(ids)
            self.periodic_s = float(self.s.semantic_check_sec)
        else:
            self.pending_since_row = None
            if not stage_a.hit:  # quiet stream: back off the periodic paraphrase check (cue hits stay instant)
                self.periodic_s = min(self.periodic_s * 1.5, max(240.0, self.s.semantic_check_sec))

    # ---- alerts ------------------------------------------------------------------------------

    def _fire(self, d: Any, window: list[tuple[int, str]]) -> None:
        ev = self.store.rows_by_id(d.row_ids)
        if not ev:
            return
        t_pos, wall = ev[0][1], ev[0][3]
        quote = d.quote.strip() or " ".join(r[4] for r in ev)
        if len(quote) > 220:
            quote = quote[:217] + "…"
        n = len(self.alerts) + 1
        evdir = self.workdir / "evidence" / f"a{n}"
        if self.ingest:
            clips.snapshot(self.ingest.segments(), evdir, t_pos - 12, t_pos + 12)
        now = time.time()
        alert = {"n": n, "pos": t_pos, "wall": wall, "rows": d.row_ids, "quote": quote, "dir": str(evdir),
                 "unconfirmed": d.unconfirmed, "fired": now, "resnap_done": False, "clip": None}
        self.alerts.append(alert)
        arrival_latency = now - wall
        self.store.event(self.id, "alert", t_pos, n=n, rows=d.row_ids, quote=quote, unconfirmed=d.unconfirmed,
                         confidence=d.confidence, latency_from_arrival=round(arrival_latency, 1),
                         judge_latency=round(self.judge_latencies[-1], 1) if self.judge_latencies else None)
        self.store.update_watch(self.id, alerts=n)
        when = time.strftime("%H:%M", time.localtime(wall))
        head = (f"🔔 It's happening: {self.req.condition}" if not d.unconfirmed else
                f"🔔 Possibly happening (unconfirmed, cue match only): {self.req.condition}")
        link = self.src.link(t_pos if not self.src.is_live or self.src.kind == "demo" else None)
        text = (f"{head}\n“{quote}” ({when}, {sources.fmt_pos(t_pos)} into the "
                f"{'stream' if self.src.kind in {'demo', 'file'} or not self.src.is_live else 'watch'})\n"
                f"▶ Jump in: {link}\nReply “clip”, “not it” or “stop”.")
        if self.req.mode == "once":
            self.fired_at = now
            self.set_status("fired")
        else:
            self.cooldown_until = now + self.req.cooldown_min * 60
        channel = self.say(text, alert=True)
        if self.s.auto_clip:
            self.side_pool.submit(self.clip)
        if channel == "posted" and self.s.auto_attach_clip:
            self.side_pool.submit(self._attach_clip)
        self.mgr.after_alert(self, alert)

    def _attach_clip(self) -> None:
        """P1 auto-attach: posts can't carry media, so (only when the user granted
        allow_gateway_injection) ask the agent in this chat to fetch the clip with the wakeme_clip tool,
        whose result Hermes delivers as native video. Without the grant this is a no-op returning False."""
        injector = getattr(self.ctx, "inject_message", None)
        key = self.session.get("session_key")
        if injector is None or not key:
            return
        # The seconds after the moment are still being recorded at fire time; give them a moment so the
        # clip isn't cut off right after the line that triggered it.
        a = self.alerts[-1]
        deadline = time.time() + 20
        while not self._buffered_past(a["pos"] + 14) and time.time() < deadline:
            if self.stop_event.wait(0.5):
                break
        self.clip()  # cut it now so the tool call returns at once
        try:
            ok = injector(f"[Wake Me When] Watch #{self.id} just alerted. Call the wakeme_clip tool with "
                          f"watch_id {self.id} and reply with one short line describing the clip.",
                          role="user", session_key=key)
        except Exception:
            ok = False
        self.store.event(self.id, "auto_attach", self.pos, injected=bool(ok))

    def _buffered_past(self, t: float) -> bool:
        """True once the ring holds video up to stream position ``t`` (or nothing more is coming)."""
        if not self.ingest:
            return True
        segs = self.ingest.segments()
        return bool(segs) and segs[-1][2] >= t

    def _resnap(self) -> None:
        """Catch the segments after the moment once they exist (they're still being written at fire time)."""
        if not self.alerts or not self.ingest:
            return
        a = self.alerts[-1]
        if a["resnap_done"] or not self._buffered_past(a["pos"] + 14):
            return
        if not self.lock.acquire(blocking=False):
            return  # clip() is cutting this alert's clip; retry on the next loop
        try:
            clips.snapshot(self.ingest.segments(), Path(a["dir"]), a["pos"] - 12, a["pos"] + 14)
            a["resnap_done"] = True
        finally:
            self.lock.release()

    def _update_behind(self) -> None:
        self._resnap()
        if self.status not in {"watching", "behind"}:
            return
        behind = asr.WORKER.behind.get(self.id, False) or asr.WORKER.backlog(self.id) > 3
        if behind and self.status == "watching":
            self.set_status("behind")
        elif not behind and self.status == "behind":
            self.set_status("watching")

    # ---- frames ------------------------------------------------------------------------------

    def _maybe_frames(self) -> None:
        if not self.ingest or not self.ingest.frames or self.status == "fired":
            return
        now = time.time()
        if now - self.last_ocr < 4:
            return
        frame = self.ingest.latest_keyframe()
        if frame is None or not self.frame_gate.changed(frame):
            return
        self.last_ocr = now
        if self.plan.ocr_patterns:
            text = vision.ocr(frame)
            changed = cues.normalize(text) != cues.normalize(self.ocr_text)
            self.ocr_text, self.ocr_at = text, now
            res = cues.match([], [], None, text, self.plan.ocr_patterns)
            if res.ocr and changed:
                self.store.event(self.id, "ocr_hit", self.pos, text=self.ocr_text[:200])
                self._maybe_judge(trigger=True)
        if self.plan.visual_question and now - self.last_vision > 20:
            self.last_vision = now
            self.side_pool.submit(self._vision_check, frame.read_bytes(), self.pos)

    def _vision_check(self, img: bytes, pos: float) -> None:
        try:
            parsed = self.structured(vision.VISION_INSTRUCTIONS, f"Question: {self.plan.visual_question}",
                                     vision.VISION_SCHEMA, "wakeme_vision", "wakeme.vision", images=[img],
                                     max_tokens=200)
        except Exception as exc:
            self.store.event(self.id, "vision_error", pos, error=str(exc)[:200])
            return
        if isinstance(parsed, dict) and parsed.get("answer") and float(parsed.get("confidence") or 0) >= HIGH_CONF:
            what = str(parsed.get("what") or self.plan.visual_question)[:200]
            self.store.add_row(self.id, pos, pos, time.time(), f"[on screen] {what}")
            self.store.event(self.id, "vision_hit", pos, what=what)
            self.trigger_judge = True  # picked up by the watch loop (judge state is loop-owned)

    # ---- summaries / catch-up -----------------------------------------------------------------

    def _maybe_summarize(self) -> None:
        if self.sum_future is not None and not self.sum_future.done():
            return
        chunk = self.s.catchup_summary_min * 60
        if self.pos - self.sum_end < chunk + 5:
            return
        start, end = self.sum_end, self.sum_end + chunk
        rows = [r for r in self.store.rows(self.id, since_pos=start) if r[1] < end]
        self.sum_end = end
        if summary.word_count(rows) < summary.MIN_WORDS:
            self.store.add_summary(self.id, start, end, "")
            return

        def run() -> None:
            try:
                text = self.complete(summary.SUMMARY_INSTRUCTIONS, summary.rows_text(rows), "wakeme_judge",
                                     "wakeme.summary", max_tokens=300)
            except Exception:
                text = ""
            self.store.add_summary(self.id, start, end, text)

        self.sum_future = self.side_pool.submit(run)

    def catchup(self, minutes: Optional[float] = None) -> str:
        if minutes:
            since, label = max(0.0, self.pos - minutes * 60), f"last {minutes:g} min"
        elif self.alerts:
            since, label = max(0.0, self.alerts[-1]["pos"] - 30), "since the last alert"
        else:
            since, label = 0.0, "since the watch started"
        sums = [s for s in self.store.summaries(self.id) if s[1] > since and s[2]]
        raw_from = max(since, sums[-1][1] if sums else since)
        raw = self.store.rows(self.id, since_pos=raw_from)
        title = self.src.title if self.src else self.req.url
        head = f"📺 Catch-up · watch #{self.id} · {title} ({label})"
        if not sums and not raw:
            return f"{head}\nNothing has been said yet."
        try:
            body = self.complete(summary.CATCHUP_INSTRUCTIONS, summary.compose_input(self.req.condition, sums, raw),
                                 "wakeme_judge", "wakeme.catchup", max_tokens=600)
        except Exception:
            body = ""
        return f"{head}\n{body or summary.fallback(sums, raw)}"

    # ---- user actions -------------------------------------------------------------------------

    def not_it(self, note: str = "") -> str:
        with self.lock:
            if not self.alerts:
                return f"Watch #{self.id} hasn't alerted yet."
            a = self.alerts[-1]
            self.deb.reject(a["rows"])
            self.negatives.append(a["quote"] + (f" (user: {note})" if note else ""))
            self.store.event(self.id, "feedback", a["pos"], verdict="not_it", rows=a["rows"], note=note)
            self.fired_at = None
            self.cooldown_until = 0.0
            if not self.stop_event.is_set():
                self.set_status("watching")
            return f"👍 Got it, that wasn't it. Watch #{self.id} keeps watching for: {self.req.condition}"

    def clip(self) -> tuple[Optional[Path], str]:
        """Cut the clip for the latest alert (from its saved evidence) or, with no alert, the last
        ``clip_seconds`` of the live ring. Returns (path, message)."""
        with self.lock:
            ffmpeg = sources.which("ffmpeg")
            if not ffmpeg:
                return None, "ffmpeg is not installed."
            out_dir = data_dir() / "clips"
            if self.alerts:
                a = self.alerts[-1]
                msg = f"🎬 Clip for watch #{self.id}: “{a['quote'][:90]}” ({sources.fmt_pos(a['pos'])})"
                if a.get("clip") and Path(a["clip"]).exists():
                    return Path(a["clip"]), msg
                start = a["pos"] - min(6.0, self.s.clip_seconds / 3)
                # Only keep a clip that covers the whole window; one cut before the rest was recorded
                # is sent as is but cut again next time.
                complete = a["resnap_done"] or self._buffered_past(start + self.s.clip_seconds)
                if self.ingest and not a["resnap_done"]:
                    clips.snapshot(self.ingest.segments(), Path(a["dir"]), a["pos"] - 12, a["pos"] + 14)
                segs = clips.load_snapshot(Path(a["dir"]))
                out = clips.cut(ffmpeg, segs, start, self.s.clip_seconds, out_dir / f"w{self.id}_a{a['n']}.mp4")
                if out:
                    if complete:
                        a["clip"] = str(out)
                    return out, msg
                return None, "The saved evidence for that alert is gone (the watch has ended)."
            if not self.ingest:
                return None, f"Watch #{self.id} has no video buffered yet."
            segs = self.ingest.segments()
            if not segs:
                return None, f"Watch #{self.id} has no video buffered yet."
            tmp = self.workdir / "evidence" / f"now_{int(time.time())}"
            end = segs[-1][2]
            saved = clips.snapshot(segs, tmp, end - self.s.clip_seconds - 1, end)
            out = clips.cut(ffmpeg, saved, end - self.s.clip_seconds, self.s.clip_seconds,
                            out_dir / f"w{self.id}_{int(time.time())}.mp4")
            shutil.rmtree(tmp, ignore_errors=True)
            if out:
                return out, f"🎬 Last {self.s.clip_seconds} s of watch #{self.id}"
            return None, "Couldn't cut a clip from the buffer."

    # ---- teardown -----------------------------------------------------------------------------

    def _cleanup(self, status: str, note: str) -> None:
        if self.ingest:
            self.ingest.stop()
        asr.WORKER.drop(self.id)
        self.judge_pool.shutdown(wait=False, cancel_futures=True)
        self.side_pool.shutdown(wait=False, cancel_futures=True)
        self.store.update_watch(self.id, status=status, note=note or self.stop_reason, ended=time.time())
        self.status = status
        for sub in ("seg", "kf"):
            shutil.rmtree(self.workdir / sub, ignore_errors=True)
        # evidence/ stays until the janitor sees the fired grace window has passed
        self.mgr.forget(self)


class Manager:
    """Process-wide registry of this process's watches."""

    def __init__(self, ctx: Any, store: Store, settings_fn, tasks: set[str]):
        self.ctx = ctx
        self.store = store
        self.settings_fn = settings_fn
        self.llm = LLM(ctx, tasks)
        self.watches: dict[int, Watch] = {}
        self.lock = threading.Lock()
        self._asr_thread: Optional[threading.Thread] = None
        self.alert_hooks: list = []

    def warm_asr(self) -> None:
        with self.lock:
            if asr.ready() or (self._asr_thread and self._asr_thread.is_alive()):
                return
            name = self.settings_fn().whisper_model
            self._asr_thread = threading.Thread(target=asr.load_model, args=(name,), name="wakeme-asr-load",
                                                daemon=True)
            self._asr_thread.start()

    def create(self, req: planmod.WatchRequest, kind: str, session: dict) -> Watch:
        s = self.settings_fn()
        with self.lock:
            live = self.store.count_live()
            if live >= s.max_watches:
                raise RuntimeError(f"{live} watches are already running (max_watches = {s.max_watches}). "
                                   "Stop one with /wakeme stop <id>.")
            wid = self.store.create_watch(req.url, kind, req.condition, req.mode, session.get("session_key", ""),
                                          session.get("session_id", ""), session.get("platform", ""))
            w = Watch(self, wid, req, kind, session)
            self.watches[wid] = w
        self.janitor()
        w.start()
        return w

    def get(self, wid: int) -> Optional[Watch]:
        return self.watches.get(wid)

    def forget(self, w: Watch) -> None:
        with self.lock:
            self.watches.pop(w.id, None)

    def after_alert(self, w: Watch, alert: dict) -> None:
        for hook in self.alert_hooks:
            try:
                hook(w, alert)
            except Exception:
                log.debug("wake-me-when: alert hook failed", exc_info=True)

    def stop(self, wid: int, reason: str = "stopped by you", wait: float = 10.0) -> str:
        w = self.watches.get(wid)
        if w is not None:
            w.request_stop(reason)
            w.ended.wait(wait)
            return f"⏹ Stopped watch #{wid}."
        row = self.store.watch(wid)
        if row is None:
            return f"There's no watch #{wid}."
        if row["status"] not in ("planning", "watching", "behind", "fired"):
            return f"Watch #{wid} already {row['status']}."
        if row.get("owner") != OWNER_TOKEN and owner_alive(row.get("owner") or ""):
            return f"Watch #{wid} is running in another Hermes process; stop it there."
        self.store.update_watch(wid, status="stopped", note="owner gone", ended=time.time())
        return f"⏹ Watch #{wid} marked stopped (its process is gone)."

    def stop_all(self, reason: str = "stopped by you", wait: float = 10.0) -> list[int]:
        ids = list(self.watches)
        for wid in ids:
            self.watches[wid].request_stop(reason)
        deadline = time.time() + wait
        for wid in ids:
            w = self.watches.get(wid)
            if w is not None:
                w.ended.wait(max(0.1, deadline - time.time()))
        return ids

    def janitor(self) -> None:
        """Drop evidence of ended watches after the grace window, old clips, old transcripts."""
        s = self.settings_fn()
        root = data_dir()
        grace = s.feedback_grace_min * 60
        for d in (root / "watches").glob("w*") if (root / "watches").exists() else []:
            try:
                wid = int(d.name[1:])
            except ValueError:
                continue
            if wid in self.watches:
                continue
            row = self.store.watch(wid)
            ended = (row or {}).get("ended") or 0
            if row is None or (ended and time.time() - ended > grace):
                shutil.rmtree(d, ignore_errors=True)
        for clip in (root / "clips").glob("*.mp4") if (root / "clips").exists() else []:
            try:
                if time.time() - clip.stat().st_mtime > s.keep_clips_days * 86400:
                    clip.unlink()
            except OSError:
                pass
        try:
            self.store.prune(s.keep_transcripts_days)
        except Exception:
            log.debug("wake-me-when: prune failed", exc_info=True)

    def evidence_clip(self, wid: int) -> tuple[Optional[Path], str]:
        """Clip for a watch that has ended but whose evidence is still inside the grace window."""
        ffmpeg = sources.which("ffmpeg")
        alerts = self.store.events(wid, "alert")
        if not alerts or not ffmpeg:
            return None, f"Watch #{wid} has ended; there's nothing to clip."
        a = alerts[-1]
        done = data_dir() / "clips" / f"w{wid}_a{a['n']}.mp4"
        if done.exists():
            return done, f"🎬 Clip for watch #{wid} (alert {a['n']})"
        evdir = data_dir() / "watches" / f"w{wid}" / "evidence" / f"a{a['n']}"
        segs = clips.load_snapshot(evdir)
        out = clips.cut(ffmpeg, segs, a["pos"] - 6, self.settings_fn().clip_seconds, done) if segs else None
        if out:
            return out, f"🎬 Clip for watch #{wid}: “{a.get('quote', '')[:90]}”"
        return None, f"Watch #{wid} has ended and its buffer is gone."
