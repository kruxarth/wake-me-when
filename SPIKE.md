# Spike results (Nov 7, 2026)

Machine: i7-13620H (10C/16T), 15 GB RAM, no GPU. Hermes v0.23.0 (`2bd378ed`), Python 3.14.9.
Scripts live in `../spike/` (not part of the plugin).

**Verdict: GO.** Every load-bearing assumption holds; two surface details differ from the brief
(see 1e) and change `notify.py` and the clip path.

## 1a. Ingest (one ffmpeg, three outputs, reader thread draining `pipe:1`)

| Source | How | Audio delivered / wall | ffmpeg CPU (re-encoding segments) | RSS |
|---|---|---|---|---|
| YouTube live (NASA ISS, 2 min) | `yt-dlp -g -f "bv*[height<=480]+ba"` → 2 HLS inputs (video, audio) | 128.5 s / 123.8 s | 33 % of one core | 277 MB |
| Twitch live (summit1g, 2 min) | `streamlink -O -- URL 480p,best \| ffmpeg -i pipe:0` | 115.9 s / 120.9 s | 111 % of one core | 345 MB |

- YouTube live serves **separate audio and video HLS playlists**, so ingest takes 1 or 2 inputs.
- YouTube resolved without cookies in ~3.7 s. yt-dlp warns "No supported JavaScript runtime"
  (Deno is the default; `bun` works with `--js-runtimes bun`). Live HLS still resolved without one.
- streamlink 8.6: `--twitch-disable-ads` is now a deprecated no-op (ad filtering is built in). Not passed.
- Decision: segments are written with **`-c copy`** (no re-encode) to stay well under one core per
  watch; the clip is re-encoded only when it is cut.

## 1b. faster-whisper on this CPU (int8, VAD on, 10 s windows, beam 1)

| Model | cpu_threads | s per 10 s window | RTF | ≈ real-time streams one model can serve |
|---|---|---|---|---|
| tiny | 4 | 0.40 | 0.040 | 25 |
| base | 4 | 0.55 | 0.055 | 18 |
| small | 4 | 1.47 | 0.147 | 7 |

(8 threads was slower for every size.) Default: `base`, `cpu_threads=4`, one shared model and one
worker thread. Three watches use ~17 % of that worker's capacity. Transcripts of the demo were
word-accurate with `base`.

## 1c. Judge prompt through the user's model (`hermes chat -q`, deepseek-flash)

| Window | Result | Row ids | Wall time (incl. CLI + agent startup) |
|---|---|---|---|
| true positive (price spoken) | matched, 0.99 | [43, 44] ✓ | 24.6 s |
| near miss ("we'll get to that in a moment") | not matched, 0.93 | [41] | 29.3 s |
| clear negative | (timed out at 200 s — provider was intermittently returning 500s today) | | |

Earlier today the same provider returned HTTP 500 for ~2 minutes. The plugin must treat the judge as
fallible (timeouts, Stage-A-only mode). Direct `ctx.llm` latency is measured in the plugin itself.

## 1d. Clip from the segment ring

`ffmpeg -f concat -protocol_whitelist file -i list.txt -ss 2 -t 15 -c:v libx264 -preset veryfast`
on three 4 s Twitch segments: 0.8 s wall, 10.6 s / 734 KB mp4.

## 1e. Hermes surface probe (`spike/probe`, isolated `HERMES_HOME`, stub OpenAI server)

Facts that differ from the brief or were unknown:

1. **`hermes chat` is a gateway client.** Plugins (slash commands, tools, threads) run in the gateway
   process for every surface, including the classic CLI. So every watch lives in the gateway; there
   is no separate CLI process to coordinate with. (The process-ownership columns in the db stay as a
   safety net for multi-gateway/profile setups.)
2. Slash handlers receive `session_id`, `session_key`, `platform` and `source` keywords.
   From the classic CLI, `platform == "local"` and `ctx.session_surface() == "local"`.
3. `ctx.post_message` from the handler: ok. **From a `contextvars.copy_context()` thread: ok at 5 s,
   40 s and 80 s** while a viewer is attached. With the one-shot `hermes chat -q` client (detached
   after the reply) the thread gets `no_conversation`.
4. **The classic CLI accepts the post (gateway log `ok=True`) but did not display it**, not even after
   the next turn. Alerts for CLI users therefore also land in `/watching` (pending-alert queue).
   TUI/Desktop show a notice; messaging platforms send a real message.
5. **Posted messages do not enter the conversation history** (`state.db` has 0 rows). The agent cannot
   know what a plain-text "clip" means from history, so the plugin adds a `pre_llm_call` hook that
   appends a short "active watches in this chat" note to the user's turn only while that chat has
   watches (ephemeral, cache-safe). Explicit `/wakeme clip <id>` stays the reliable path.
6. **`post_message` does not extract `MEDIA:` tags**: it calls `adapter.send(text)` directly. Clips are
   delivered from a slash-command reply or a tool result, both of which go through the adapter's
   response path (`_extract_response_content` → `extract_media`). Auto-attach from the background
   thread is therefore P1 via `inject_message` (needs `allow_gateway_injection`), not P0.
7. `inject_message` without the grant returns `False` (as documented).
8. Media delivery policy: non-strict (default) accepts any existing file outside the denylist; strict
   mode accepts freshly produced files, which lazy clip cutting satisfies.

Still to verify on a real messaging platform (needs Telegram, which is not configured on this
machine): that a slash-command reply with `MEDIA:<mp4>` arrives as a native video.

## 1f. Python and wheels

Hermes runs on its bundled CPython 3.14.9. `faster-whisper==1.2.1` (ctranslate2 4.8.5, onnxruntime
1.31.1, av 19.0.2) installs from wheels and runs on 3.14. Hermes ships this exact pin as its
`stt-whisper` extra, so the plugin lazy-installs it with the documented public
`pm.ensure_import("stt-whisper")` instead of declaring its own copy.
