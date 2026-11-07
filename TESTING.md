# TESTING

Hardware: i7-13620H (10C/16T), 15 GB RAM, no GPU, Arch Linux. Hermes v0.23.0, Python 3.14.9,
ffmpeg 9.0.x, yt-dlp 2026.10.26, streamlink 8.6.3, faster-whisper 1.2.1 (`base`, int8, 4 threads).
Model for real runs: the maintainer's `deepseek-flash` through Hermes (`hermes chat -Q -q`), Nov 7 2026.

## Unit and integration tests

```bash
python -m pytest -q tests          # 56 passed (~3 min; the e2e tests run real ffmpeg on demo://)
hermes plugins doctor . --ci       # OK: 7 tools, 1 hook
hermes plugins validate . --install-deps   # Validation passed; security scan: safe
```

Coverage: plan parsing with a stubbed LLM, cue matching (fuzzy, phonetic, stemming, negatives, OCR),
the debounce state machine (overlapping windows never count twice, `--every` never re-fires on old
evidence, confirm-twice, not-it suppression, Stage-A-only fallback), row-id → stream-time mapping and
quote fallback, transcript windowing and overlap trimming, VTT/SRT/YouTube rolling captions, the audio
ring and bounded ASR queue, clip cutting after the ring rotated past the moment, URL safety (`--`
before URLs for yt-dlp/streamlink, per-source ffmpeg `-protocol_whitelist`, no file+network mixing,
local paths refused from messaging chats, symlink escape, private addresses), an end-to-end
`demo://keynote?speed=4` watch with a stubbed LLM (alert, clip, catch-up, hook context, auto-attach
request, stop, no ffmpeg left), not-it feedback reaching the judge, LLM outage → unconfirmed alerts,
plan failure, `max_watches` across processes, orphan marking, the Twitch-style pipe process group, and
`register(ctx)` against a fake ctx matching the manifest.

## Dev-CLI end to end (isolated `HERMES_HOME`, stub model)

`hermes chat` attached in a pty, `/wakeme demo://keynote when they announce the price` at real time:

| Event | Stream time | Wall time after start |
|---|---|---|
| price spoken | 52.3 s | 52.3 s |
| row stored (captions backend) | 52.3 s | 52.7 s |
| alert posted | — | 58.8 s (**6.5 s after the moment**; stub judge 0.02 s) |
| `/wakeme clip 2` | — | 385 s (5.4 min after the alert): 14.2 s MP4 of the price moment ✓ |
| `/catchup 2`, `/wakeme stop 2` | — | ✓; no ffmpeg left (`pgrep -a ffmpeg` empty) |

`/watching` in the classic CLI showed the alert under "Alerts you may have missed" (the classic CLI
does not display plugin posts; see SPIKE.md 1e).

## Real live streams (harness: real ffmpeg + yt-dlp + faster-whisper, judge = user's model)

`spike/live_harness.py` runs the plugin's `App` outside the gateway, with `ctx.llm` answered by the
user's Hermes model and `post_message` printed.

### Run 1 — three YouTube Live news streams at once, 13.6 min, nothing should match

| Watch | Stream | Condition | Result |
|---|---|---|---|
| #1 | ABC News Live | they talk about the midterm elections | 0 alerts; 7 judge calls; catch-up: "Nothing in this window touches the midterm elections…" |
| #2 | DW News | they give the weather forecast | 0 alerts; 7 judge calls; catch-up confirms no forecast aired |
| #3 | Al Jazeera English | they announce the winner of the Eurovision song contest | 0 alerts; 7 judge calls |

- **Zero false alerts** over 3 × 13.6 min of continuous speech.
- **Never `behind`.** ASR worker busy 195 s of 816 s (24 %) for three streams.
- **CPU:** plugin process 131 % of one core on average (whisper + Python, all three watches), the three
  ffmpegs 26 % together → **≈ 52 % of one core per watch**. RSS: plugin 543 MB (incl. the whisper
  model), ffmpegs 495 MB together.
- **LLM calls:** 9 per watch in 13.6 min with `semantic_check_sec=120` (plan + 7 judge + 1 summary);
  judge calls took 3.8–5.0 s, plans 6.4–7.0 s, summaries 11–24 s.

<!-- RUN3 -->

## Latency budget

<!-- BUDGET -->

## Gateway / Telegram end to end

Not run yet: Telegram is not configured on the development machine and permission to install into the
real `~/.hermes` is pending. Steps for the maintainer:

```bash
hermes gateway setup                      # connect Telegram
hermes plugins install ./wake-me-when     # or: hermes plugins install wake-me-when (after catalog merge)
hermes plugins enable wake-me-when
hermes wakeme doctor                      # installs/warms speech recognition (stt-whisper extra)
hermes gateway restart
```

In the Telegram chat with the bot:

1. `/wakeme demo://keynote when they announce the price` → instant 👀 reply, then 📋 plan.
2. ~55–70 s later: 🔔 alert in the same chat (note the delay after 0:52).
3. `/wakeme clip 1` → a **native Telegram video** of the price moment (verifies MEDIA from a slash reply).
4. Ask the agent "send me the clip again" → it calls `wakeme_clip` → video (verifies MEDIA from a tool).
5. `/catchup 1`, `/watching`, `/wakeme stop 1`, then `pgrep -a ffmpeg` → empty.
6. Optional: `plugins.entries.wake-me-when.allow_gateway_injection: true`, restart, repeat 1–2 → the
   agent attaches the clip by itself after the alert.
