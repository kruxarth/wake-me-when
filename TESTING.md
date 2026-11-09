# TESTING

Hardware: i7-13620H (10C/16T), 15 GB RAM, no GPU, Arch Linux. Hermes v0.23.0, Python 3.14.9,
ffmpeg 9.0.x, yt-dlp 2026.10.26, streamlink 8.6.3, faster-whisper 1.2.1 (`base`, int8, 4 threads).
Model for real runs: the maintainer's `deepseek-flash` through Hermes (`hermes chat -Q -q`), Nov 7 2026.

These runs predate the single `/wakeme` command, so they use its earlier names: `/watching` is now
`/wakeme list` and `/catchup` is now `/wakeme catchup`. The harness scripts and logs cited as `spike/…`
live in the development workspace, not in this repository.

## Unit and integration tests

```bash
python -m pytest -q tests          # 122 passed (~2 min; the e2e tests run real ffmpeg on demo://)
hermes plugins doctor . --ci       # OK: 7 tools, 1 hook
hermes plugins validate . --install-deps   # Validation passed; security scan: safe
```

Coverage: plan parsing with a stubbed LLM, cue matching (fuzzy, phonetic, stemming, negatives, OCR),
the debounce state machine (re-judging the same window never counts twice, a wider window confirms, `--every` never re-fires on old
evidence, confirm-twice, not-it suppression, Stage-A-only fallback), row-id → stream-time mapping and
quote fallback, transcript windowing and overlap trimming, VTT/SRT/YouTube rolling captions, the audio
ring and bounded ASR queue, clip cutting after the ring rotated past the moment, URL safety (`--`
before URLs for yt-dlp/streamlink, per-source ffmpeg `-protocol_whitelist`, no file+network mixing,
local paths refused from messaging chats, symlink escape, private addresses, the private-address check
on stream URLs yt-dlp and streamlink resolve, exact YouTube/Twitch host matching, the network guard for
direct HLS and web-media links), the minimal tool
environment (every `subprocess` call passes `env=tool_env()`; fake tools show planted keys never arrive), an end-to-end
`demo://keynote?speed=4` watch with a stubbed LLM (alert, clip, catch-up, hook context, auto-attach
request, stop, no ffmpeg left), not-it feedback reaching the judge, LLM outage → unconfirmed alerts,
plan failure, `max_watches` across processes, orphan marking, the Twitch-style pipe process group, and
`register(ctx)` against a fake ctx matching the manifest.

## Network guard for direct HLS and web-media links (0.1.2, Nov 9)

`tests/test_guard.py` uses local HTTP servers: 127.0.0.1 is the public host and 127.0.0.2 the LAN host
(the address check is patched to refuse only 127.0.0.2), with real streamlink 8.6.3 and ffmpeg 9.0.2.

| Case | Result |
|---|---|
| control: ffmpeg given the public playlist URL itself | fetches the LAN segment (the gap 0.1.1 disclosed) |
| public playlist whose segment / AES key is on the LAN host | refused by the guard; the LAN server sees no request |
| public master playlist whose variant is on the LAN host | refused while the plugin reads the playlist, before streamlink starts |
| playlist that redirects (302) to the LAN host | refused by the plugin's own playlist read |
| public segment that redirects to the LAN host | streamlink follows it through the guard, which refuses it |
| server that answers safely first and redirects to the LAN later | the later fetch is refused (each connection is checked when it is made) |
| DNS answer with one public and one private address | refused; a public-only answer is dialled at the checked address |
| proxy request without credentials, with wrong ones, or with a released ticket | `407` |
| same-host 6 s VOD playlist | plays through `Ingest`: 6 s of audio read in real time; VOD and duration come from `#EXT-X-ENDLIST` |
| find on that VOD | 6 s of 16 kHz PCM through the pipe |
| MP4 with `moov` after `mdat` | clear "index at the end" reason; a faststart MP4 opens through `httpstream://` |
| watch on a playlist with LAN segments | stops with "❌ Watch #1 stopped: the stream asked for an address on a private or local network…" |
| `allow_private_network` on, or `HTTPS_PROXY` set | ffmpeg reads the link directly, as before |

Live, with Hermes's own `tools.url_safety` (`spike/live10.log`, `spike/find_guard.log`):

- `https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8`: a VOD (634.6 s, from the playlist) played through
  streamlink and the guard at real time. A 3-minute watch with the real model and faster-whisper posted the
  plan card, transcribed the stream's few words, ran a judge check and a catch-up, refused nothing,
  and left no ffmpeg or streamlink after stop (plugin 22 % of a core on average, ffmpeg and streamlink 20 %).
- find on the same VOD read and searched all 10.5 minutes in 23.7 s (no matching moment for the question).
- find on the NASA Artemis I MP4 (below) through `httpstream://`: the same answer as before,
  **1:33** "…barreling its way back home…", 12.9 s in total.

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
does not display plugin posts).

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

### Run 2 — LiveNOW FOX, 9 min, "they mention Trump"

0 mentions of Trump in 147 transcript rows → 0 alerts (correct). Its Twitch watch hit a process-group
bug (streamlink + ffmpeg, `EPERM`), fixed in "Fix Twitch process group".

### Run 3 — two live streams + the demo at real time, real model, 10.6 min (`spike/live3.log`)

| Watch | Stream | Condition | Result |
|---|---|---|---|
| #1 | LiveNOW from FOX (YouTube Live) | they talk about the jobs report or the economy | 0 alerts, 11 verdicts. The segment was a speech on Detroit school funding and military spending, which the catch-up called out; no jobs-report talk. Correct negative. |
| #2 | xQc (Twitch, GTA RP) | they talk about GTA | 0 alerts, 7 verdicts. Speech was in-game roleplay that never names the game. Correct negative. |
| #3 | `demo://keynote`, real time | they announce the price | **alert** "Acme Phone X starts at just $799." at 0:51 stream time; plan chose `confirm: twice` → 3 judge calls (0.97 each). `/wakeme clip 3` 72 s later: 20.0 s MP4 of the price slide. Catch-up put the price first. |

ASR worker busy 82 s of 638 s (13 %). No watch went `behind`. Judge calls 3.6–7.9 s (median 4.2 s),
plans 5.6–8.4 s.

### Run 4 — three live streams, 10.7 min (`spike/live4.log`) — found a confirmation bug

| Watch | Stream | Condition | Result |
|---|---|---|---|
| #1 | LiveNOW from FOX | they talk about taxes or government spending | At 9:05 a speaker said "We want an America where our tax dollars are spent to invest in our fellow Americans rather than to buy bombs and tanks…". The judge matched it **9 times in a row at 0.90–0.95**, but no alert fired (**bug**, below). |
| #2 | xQc (Twitch) | someone talks about a car or driving | "what is she pinged that car earlier?" → 3 positives at 0.62–0.72, held as pending (borderline passing mention; see below). |
| #3 | Sky News (YouTube Live) | they give the weather forecast | A polar-vortex/El Niño explainer → one positive at 0.66, then 0.30. No forecast was read. Correct negative. |

**Bug found and fixed (commit "Confirm a pending positive when a wider window still agrees").** With
`confirm: twice`, a second positive had to cite a row the first did not. A one-sentence moment that
every later, wider window keeps citing could therefore never confirm. Now a second positive also
confirms when its window reaches newer speech than the pending one did. Re-judging the same window
(a cue re-trigger with no new speech) still never counts twice. Replaying run 4's recorded verdicts
through the fixed debouncer (`tests/test_judge.py::test_same_rows_confirmed_by_a_window_that_saw_newer_speech`):

- watch #1 fires on its 2nd positive (whose window already held newer rows 277–284), 14–16 s after the "tax dollars" rows were stored;
- watch #2 would have fired on "that car" at 0.72. That's a correct, if minor, match for "someone
  talks about a car".

### Run 5 — three Twitch streams, 10.5 min (`spike/live5.log`)

The first attempt at 14:44 showed the failure messages working. YouTube refused both news streams
("YouTube asked to sign in to confirm you're not a bot; this plugin never reads browser cookies") after
a day of test runs from this IP, and xQc had gone offline ("No playable streams found on this URL").
Each watch ended with one clear ❌ message and no process left behind.

| Watch | Stream | Condition | Result |
|---|---|---|---|
| #1 | StableRonaldo (Fortnite) | they open a chest | 0 alerts, 9 verdicts. He spent the whole window *digging up* a chest. The judge cited "I'm digging the chest up" as evidence and still said `matched: false` (0.12–0.2), and the catch-up says "The chest is still unopened". Correct negative on a near miss. |
| #2 | Kai Cenat (GTA SA) | someone talks about a car or driving | 1 row in 10 min: the stream audio was silent (mean −80 dB on a 20 s sample afterwards). Correct. |
| #3 | Sodapoppin (WoW) | they talk about gold or money | 0 alerts, 4 verdicts; catch-up: "No gold or money talk in this stretch". |

### Run 6 — three Twitch streams, 12.4 min (`spike/live6.log`)

| Watch | Stream | Condition | Result |
|---|---|---|---|
| #1 | StableRonaldo | they talk about the pirate token | 0 rows: no speech for 12 min (game audio only; a 20 s sample afterwards transcribed to nothing). Catch-up: "Nothing has been said yet." Correct. |
| #2 | Sodapoppin (WoW) | someone mentions a dungeon, raid or boss | 0 alerts, 5 verdicts; catch-up: "No dungeon, raid or boss has been mentioned… The closest thing is an ordinary bear kill". |
| #3 | summit1g (WoW) | they plug a sponsor or a discount code | 0 alerts, 5 verdicts; catch-up: "No sponsor or discount code has been plugged yet". |

CPU: plugin 81 %, media processes 165 % → ≈ 82 % of a core per Twitch watch; RSS 504 MB / 959 MB;
ASR busy 16 %.

### Run 7 — two Twitch streams, 10.5 min (`spike/live7.log`)

| Watch | Stream | Condition | Result |
|---|---|---|---|
| #1 | summit1g | he thanks someone for a donation, a sub or bits | "Thank you Desmond. Appreciate you guys so much for support me…" (0:00) → judge 0.62; "thank you, brother." (6:50) → 0.66, then 0.62. The plan's floor was 0.65 with `confirm: twice`, so no alert. These are vague thank-yous that never say what was given, so holding back is defensible. The catch-up led with them. |
| #2 | Sodapoppin | he talks about leveling up or choosing a talent | "level 30 I know travel form" at 9:44 matched none of the plan's cues ("level up", "next level", …). The adaptive backoff had stretched the periodic judge to ~5 min, so the run ended before the next one. **Limitation:** a moment that matches no cue waits for the periodic judge (1–4 min). The catch-up put it first. |

CPU: plugin 53 %, media processes 105 % → ≈ 79 % of a core per Twitch watch; ASR busy 10 %.

### Run 8 — two Twitch streams, 10.6 min (`spike/live8.log`) — **true-positive live alert**

| Watch | Stream | Condition | Result |
|---|---|---|---|
| #1 | Sodapoppin (WoW) | they go into a dungeon | 0 alerts, 4 verdicts. The group formed "for the dungeon" but hadn't entered by the end (catch-up: "Group forming for the dungeon… heading out"). Correct negative. |
| #2 | summit1g (WoW) | he talks about a quest he is doing | **🔔 alert, correct.** Verbatim: `🔔 It's happening: he talks about a quest he is doing ⏎ “Fuck we should just do the other quest, whatever that was.” (15:33, 3:55 into the watch) ⏎ ▶ Jump in: https://twitch.tv/summit1g`. Clip 70 s later: 20.0 s, 1920×1080 H.264 + AAC; speech recognition on the clip hears "…we should have just done the other quest…" in its first 7 s. The catch-up confirms it: "[3:52] Bails on the current plan and decides to go do 'the other quest' instead". |

Plan: `confirm: twice`, `min_confidence: 0.6`. Both confirming verdicts cited the same row (65) from
successive windows, so this alert fired *because of* the run-4 fix; the old rule would have kept it
pending. Earlier, more passing quest mentions ("it gives you a quest at the end when every mob is dead",
0.55–0.72) were broken up by negatives and did not fire.

CPU: plugin 72 %, media processes 63 % → ≈ 68 % of a core per Twitch watch; RSS 492 MB / 492 MB;
ASR busy 13 %. `PGREP after stop: (none)`.

**False alerts across runs 1–8: zero.** That's 19 live watches and ~3.5 h of watch time.

### Resources (runs 3–5, three watches each, `semantic_check_sec=60`)

| Run | Plugin process CPU | ffmpeg/streamlink CPU | per watch | RSS plugin / media procs | ASR busy |
|---|---|---|---|---|---|
| 3 (2 live + demo)\* | 60 % | 95 % | ≈ 52 % of a core | 357 MB / 711 MB | 13 % |
| 4 (3 live)\* | 98 % | 67 % | ≈ 55 % | 379 MB / 647 MB | 18 % |
| 5 (3 Twitch) | 75 % | 139 % (incl. streamlink) | ≈ 71 % | 448 MB / 662 MB | 15 % |

\* Runs 3 and 4 overlapped for ~6 minutes on the same machine, so their CPU/RSS figures include each
other's processes (run 3 saw 7 ffmpegs). Run 3's `PGREP after stop` lists run 4's processes (paths under
`harness-data4`); run 4's own check after stop is `(none)`. For per-watch cost use run 1 (≈ 52 % of a core
per YouTube watch) and runs 5–8 (68–82 % per Twitch watch, streamlink included), which ran alone.

**LLM calls:** 2–11 per watch-hour (`/watching` shows it live) with `semantic_check_sec=60`; the
adaptive backoff stretches quiet watches to one periodic judge call every 4 min. Each watch also
makes 1 plan call, and 1 summary call per 10 min of speech.


## find in recordings (`spike/find_real.py`: plugin `vod.find`, rerank = the user's model)

| Recording | Question | Path | Result | Ground truth |
|---|---|---|---|---|
| `demo://keynote` | when do they announce the price | captions | 1. **0:52** "So, how much is it? Acme Phone X starts at just seven hundred and ninety nine dollars." | caption 0:52.3 ✓ |
| `demo://launch` | when do they hold the countdown | captions | 1. **0:16** "We are now holding at T minus five minutes…" | caption ✓ |
| YouTube `8dHwQq0GrAA` (Artemis I explainer) | splashdown in the pacific | YouTube captions | 1. **5:00** "so it can safely splash down into the Pacific Ocean", link `&t=297s` | ✓ (keyword-only mode gives the same hit) |
| NASA image library MP4 "Artemis I Moon Mission – Launch to Splashdown Highlights" (public domain, over https) | when does Orion splash down | local speech recognition (no captions for web media) | 1. **1:33** "…spacecraft is barreling its way back home... Splashdown." (link at 1:30), 15.8 s total | NASA's SRT: "barreling its way back home" 1:33.8, "Splashdown." 1:45.7 → lands on the 12 s descent lead-in |

Bug found here and fixed (commit "find: spell out the rerank JSON shape…"): with no schema enforcement
the model answered `{"id": …}` instead of `{"passage_id": …}`, and find dropped every hit ("No
matching moment found"). The prompt now spells out the shape, and `id` is accepted.

## Latency budget

Measured on run 3's demo watch (real time, real model; `spike/harness-data3/data.db`, events of
watch #3):

| Step | Time | Measured |
|---|---|---|
| Price sentence spoken | 0:53.5 stream time | row 34 "Acme Phone X starts at just $799." |
| → transcript row stored (10 s window, 2 s overlap, whisper `base`) | +0.1 s after the sentence ends (median over the watch) | row wall times vs. stream position |
| → first judge call starts (cue / OCR "$799" trigger, or the periodic check) | +6–8 s | verdict at 63.0 s, 4.2 s call |
| → judge calls (3 × ~4 s, plan chose `confirm: twice`; the second cited no new row, which the fix above would now accept) | +13 s | 4.18 s, 4.13 s, 3.95 s |
| **Alert** | **~25 s after the price was spoken; 22.9 s after its row was stored** | `latency_from_arrival: 22.9` |

With `confirm: once` and a confident judge it is one call: window + ~0.5 s ASR + ~4 s ≈ 6–12 s
(6.5 s measured with the stub model in the dev CLI).

**Live (run 8, summit1g on Twitch, watch #2; `spike/harness-data8/data.db`, times from watch start):**

| Step | Watch time | Δ |
|---|---|---|
| "…we should just do the other quest…" spoken | stream position 3:52–3:55 | — |
| transcript row 65 stored | 250.6 s | ~15 s (10 s window; position counts from the first audio byte, so this also holds streamlink's start-up) |
| first positive verdict (0.72), call 5.1 s | 263.9 s | +13 s (the next call started at 258.8 s, as soon as the previous negative call returned) |
| confirming verdict (0.62), call 4.5 s | 268.7 s | +4.8 s |
| **🔔 alert** | **268.7 s** | **18.1 s after the row was stored, ~33 s after the speech position**, plus Twitch's own ~5–10 s broadcast delay |

**Per source:** add how far the source runs behind real life. `demo://` and files: 0. Twitch: ~5–10 s.
YouTube Live: ~15–30 s (HLS). RTSP: ~1–3 s. After the transcript row, the pipeline adds 6–25 s on any source (18–23 s measured with a real model and `confirm: twice`). On
live streams the transcript lag was 0–13 s (median 0.1 s on YouTube, 9.6 s on Twitch, measured as
row wall time minus stream position).


## Stop, disable, unload: no processes left (isolated `HERMES_HOME=.hermes-dev`, Nov 7 15:46)

A real-time `demo://keynote` watch started through `hermes chat` (`spike/cleanup_*.log`):

```text
--- 15:46:35 pgrep -a ffmpeg while watching:
1880508 …/ffmpeg -hide_banner -loglevel error -nostdin -readrate 1 -protocol_whitelist file -i /…/keynote.mp4 …
--- hermes plugins disable wake-me-when
⊘ Plugin wake-me-when disabled.
The running gateway reloaded its plugins and unloaded wake-me-when; …
--- 15:46:49 pgrep -a ffmpeg after disable:
(empty)

--- 15:48:18 pgrep -a ffmpeg while watching:
1888925 …/ffmpeg -hide_banner -loglevel error -nostdin -readrate 1 -protocol_whitelist file -i /…/keynote.mp4 …
--- SIGTERM to the dev gateway (spike/devgw_stop.sh)
stopped dev gateway 1876623
--- 15:48:20 pgrep -a ffmpeg after gateway SIGTERM:
(empty)
```

`/watching` afterwards: `Recent: #6 stopped (gateway restart) · #5 stopped (plugin unloaded) · …`.
`/wakeme stop` is covered in the dev-CLI run above and in every harness run (`PGREP after stop: (none)`,
including the Twitch runs with streamlink).

## Gateway / Telegram end to end

Run on Nov 7 against the maintainer's real Hermes v0.23.0 gateway (systemd user service, Telegram in
polling mode, model deepseek-flash). The plugin was installed the way a user installs it from Git:
`hermes plugins install file://…/wake-me-when --ref <sha> --enable`, then
`hermes config set plugins.entries.wake-me-when.allow_gateway_injection true`, `hermes wakeme doctor`
(installed `stt-whisper`, Whisper `base` ready in 32 s, demo watch heard the "dollars" cue, judge call
2.7 s) and `hermes gateway restart`. Messages were typed in the bot's DM by the maintainer.

Final run (watch #4, plugin at `1c7c7f2`). Times are gateway log timestamps:

| Step | Result |
|---|---|
| `/wakeme demo://keynote when they announce the price` (18:12:00) | 👀 reply at once; 📋 plan at 18:12:06 (plan call 5.9 s) |
| Alert | 🔔 at 18:13:16, quote "Acme Phone X starts at just $799." at 0:53; 16.6 s after the price audio arrived, last judge call 1.5 s |
| Auto-attach (`allow_gateway_injection`) | injected turn → agent called `wakeme_clip` → **native Telegram video** at 18:13:21 (5 s after the alert), 20.0 s long |
| `/wakeme clip 4` | **native Telegram video** (slash-command reply with MEDIA), delivered 18:13:37 |
| "send me the clip of that" | agent called `wakeme_clip` → **native Telegram video** 3 s after the message |
| `/catchup 4` | timestamped bullets 0:00–0:56 including "[0:53] Price announced …" (one model call, 12 s) |
| `/watching` | `#4 … "they announce the price" · fired · up 2:47 · 1 alert(s)` plus the recent ended watches |
| `/wakeme stop 4` | ⏹ stopped; `pgrep -a ffmpeg` and `pgrep -a streamlink` empty afterwards |

Bugs found on Telegram and fixed (each with a regression test, 59 tests pass):

- **Watch crashed right after the alert** (watch #1: "❌ Watch #1 stopped: Expecting value: line 1
  column 1"). The post-alert resnap read the evidence `index.json` while the auto-attach clip on the
  side pool was rewriting it. Resnap now takes the watch lock (without blocking the loop), index writes
  are atomic (`os.replace`) and an unreadable index counts as empty. Fixed in `4cb7ad0`.
- **Auto-attached clip cut short** (watch #2: 13.8 s instead of 20 s). It was cut at fire time before the
  seconds after the moment were recorded, and that short cut was cached; resnap also triggered on audio
  position, not on buffered video, so it could freeze one segment early (18 s in the e2e test). The
  auto-attach now waits up to 20 s for the window to be buffered, resnap waits for buffered video, and a
  clip cut before its window was complete is never reused. Fixed in `de41296`.
- **`/wakeme stop` during the post-alert grace window posted "🏁 the stream ended"** and recorded the
  watch as ended (watch #4). It now ends as `stopped` with no extra post. Fixed in `1c7c7f2`.

Seen but harmless: the agent once tried `skill_view("wake-me-when")` (plugin skills are namespaced
`wake-me-when:wake-me-when`), got "not found", and went on to call `wakeme_clip` correctly. Telegram's
command menu holds 60 commands and hid `/wakeme` among 52 overflow commands on this install; typing it
works.

To repeat on another install:

```bash
hermes gateway setup                      # connect Telegram
hermes plugins install wake-me-when       # after catalog merge; or a Git URL with --ref
hermes plugins enable wake-me-when
hermes config set plugins.entries.wake-me-when.allow_gateway_injection true   # optional auto-attach
hermes wakeme doctor                      # installs/warms speech recognition (stt-whisper extra)
hermes gateway restart
```

Then send the steps in the table above in the bot's chat.
