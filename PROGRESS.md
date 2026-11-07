# PROGRESS — Wake Me When

Running log so another session can pick up. Newest state first in each section.

## HANDOFF (Nov 7, ~14:30 IST) — start here

Read BUILD_PROMPT.md (../BUILD_PROMPT.md) for the full brief, then this file, SPIKE.md and TESTING.md.

**Uncommitted at handoff:** adaptive periodic-judge backoff in `wakeme/watcher.py` (`periodic_s`: x1.5 per
clear negative up to 4 min, reset on cue hit/pending), README tweaks, `docs/src/screens.html`. Run the
suite before committing: `../spike/.venv/bin/python -m pytest -q tests` (56 tests, ~3 min; output of the
last run: /tmp/claude-1000/-home-krutarth-coding-hackathon-hermes-plugin-hackathon/f5385294-cd06-40c3-93a3-ba9993604983/tasks/bks3mia8l.output).

**Background runs still going (they write to disk; just read the logs when done — "PGREP after stop" is the last line):**
- `../spike/live3.log` (+ `live3.log.catchup*.txt`, db `../spike/harness-data3/`): LiveNOW FOX "jobs report
  or economy", xQc Twitch "GTA", demo://keynote real-time with the user's real model. Already got: real-model
  demo alert ~22.9 s after the price row (plan chose confirm=twice; 3 verdicts), clip 20.0 s cut 72 s after.
  Live streams had no matching speech so far.
- `../spike/live4.log` (db `../spike/harness-data4/`): LiveNOW "taxes or government spending", xQc "a car or
  driving", Sky News "weather forecast" — aiming for the first TRUE-POSITIVE live alert (DoD item still open).
- Harness: `../spike/live_harness.py <minutes> <log> "url|condition" ...` (PATH=../spike/.venv/bin:$PATH,
  WAKEME_DATA_DIR=<fresh dir>, SEM_SEC=60). Judge = user's model via `hermes chat -Q -q` (4–8 s/call today).

**Remaining work, in order:**
1. Read live3/live4 results; fill TESTING.md `<!-- RUN3 -->` and `<!-- BUDGET -->` (latency budget per source:
   window ≤10 s + ASR ~0.5 s + judge ~4 s (+ ~8 s and one more judge when confirm=twice) + source delay).
   Run 2 (live2.log): 9 min LiveNOW "Trump", 0 Trump mentions in 147 rows → 0 alerts (correct); its Twitch
   watch crashed on the EPERM bug, since fixed (commit "Fix Twitch process group").
   If no live true positive yet, run another harness with a near-certain condition (news: "they mention a US
   state"; Twitch talkers) and record it.
2. Screenshots: `python ../spike/render_screens.py data.json` renders docs/screenshots/{alert,find}.png from
   docs/src/screens.html using VERBATIM real output (plan/alert/clip from live3 demo watch #3; clip frame:
   ffmpeg -ss 5 on ../spike/harness-data3/clips/w3_a1.mp4 → docs/src/clip_frame.jpg; find output from the
   YouTube 8dHwQq0GrAA "splashdown in the pacific" run, see TESTING/README). Label them as real dev-run output.
3. Update README "Expected latency" with measured numbers; DEMO_SCRIPT note says alert lands ~6–20 s → use
   measured ~20–25 s with a real model.
4. Commit; re-run `HERMES_HOME=../.hermes-dev hermes plugins validate . --install-deps` and
   `hermes plugins doctor . --ci`; tag the submittable state (`git tag v0.1.0`).
5. Fill `<TELEGRAM_RESULT>` in ../submission/PR_DESCRIPTION.md only after the user tests Telegram
   (TESTING.md "Gateway / Telegram" steps). Telegram is NOT configured; the permission question
   (install into real ~/.hermes) is still unanswered — don't touch ~/.hermes, never push/post/submit.
6. Final report per BUILD_PROMPT §9.

**Verified already:** stop / plugin disable (on_unload) / dev-gateway SIGTERM leave no ffmpeg; `/watching`
shows "stopped (gateway restart)" and "stopped (plugin unloaded)" correctly.

## Status (Nov 7, 14:20 IST)

P0 is built and works end to end; 56 tests pass; `hermes plugins doctor --ci` and
`hermes plugins validate --install-deps` are clean (security scan: safe). Submission drafts are in
`../submission/`. Remaining: a live-stream true-positive run (in progress), TESTING.md, screenshots,
Telegram E2E (needs the user), final polish.

## Environment found (Nov 7, 13:00 IST)

- `hermes --version`: **v0.23.0** (`2bd378ed1b8d`, tag `v0.23.0`, released Nov 5), git install,
  Python 3.14.9 (Hermes-bundled CPython). `_ref/hermes-agent` is cloned at the same tag.
  (Pitfall: `git tag | sort -V` puts the old CalVer tags `v2026.9.x` above `v0.23.0`; use
  `gh release list`.) Hermes also ships its own ffmpeg 9.0.1 first on PATH.
- Model: `deepseek-flash` via DeepSeek. `hermes chat -q "reply with ok"` hung 120 s at 12:50 (provider
  HTTP 500s), worked at 12:56 (44 s). From 13:55 judge calls through `hermes chat -Q -q` took 4–7 s.
- Gateway: systemd user service, running. **`hermes status`: "Platforms: none configured"** — Telegram
  is not connected, so the gateway/Telegram E2E needs the user (steps in TESTING.md).
- Permission question (install into real `~/.hermes`) asked at 12:51; **no answer yet**. All work uses
  `HERMES_HOME=./.hermes-dev` + a local stub OpenAI server (`spike/stub_llm.py`), plus the live
  harness (`spike/live_harness.py`) that runs the real pipeline outside Hermes with the user's model
  via `hermes chat -Q -q`.
- **Do not run `/wakeme doctor` (install mode) in `.hermes-dev`:** `pm.ensure_import` would install
  `stt-whisper` into the *shared* Hermes environment under `~/.hermes/installs`. The dev home uses
  `stt_backend: captions`; `hermes wakeme doctor --no-install` is safe.
- To restart the dev gateway after code changes: `spike/devgw_stop.sh` (matches only the gateway whose
  HERMES_HOME is `.hermes-dev`), then any `hermes chat` re-ensures it.

## Where the docs differ from the brief

- `developer-guide/plugins/public-api.md` does not exist in v0.23.0. The importable list is `_PUBLIC` in
  `hermes_cli/plugin_validate_private_imports.py`. We import only `plugins.plugin_storage`,
  `tools.url_safety:is_safe_url`, `pm` (`ensure_import`, `InstallError`),
  `gateway.session_context:get_session_env`.
- `tools.lazy_deps` is gone; lazy installs go through `pm.ensure_import(extra)` (Hermes extras only).
  Hermes ships `stt-whisper` = `faster-whisper==1.2.1`, so the plugin uses that extra and declares no
  Python dependencies of its own.
- `hermes chat` is a gateway client: plugins run in the gateway for every surface (SPIKE.md 1e). So there
  is no CLI-process watch; the owner pid+start token in the db protects against restarts/other processes.
- `ctx.post_message` doesn't deliver `MEDIA:`; posts aren't in conversation history; the classic CLI
  doesn't display posts. → local alerts also queue for `/watching`; a `pre_llm_call` hook gives the agent
  watch context; clips go through `/wakeme clip` / `wakeme_clip` replies; auto-attach (P1) asks the agent
  via `inject_message` when the user granted `allow_gateway_injection`.
- `ctx.session_surface()` returns `"local"` for the classic CLI; slash handlers get `platform="local"`.
- Catalog admission has 18 rules; the brief's "public-api.md" and catalog rules map to rules 9, 11, 13, 14, 17.
- streamlink 8.6: `--twitch-disable-ads` is a deprecated no-op; not passed.

## Decisions

| Decision | Why |
|---|---|
| faster-whisper `base`, int8, 4 threads, one shared worker | RTF 0.055 → ~18 streams per model (SPIKE 1b); 3 live streams used 24 % of the worker |
| Segments `-c:v copy -c:a aac` | Re-encoding cost 33–111 % of a core; copy keeps ffmpeg at ~9 % per watch |
| Demo assets at **10 fps, no B-frames** | ffmpeg `-readrate` (demo `?speed=`) mis-paces 5 fps video when a video output is mapped (62 s instead of 18 s at 4x) |
| yt-dlp / streamlink as user-installed CLIs, detected with install hints | Not Hermes extras; zero plugin deps; "no bundled binaries" |
| streamlink in a new **process group of our session** (not a new session) | ffmpeg can't setpgid into another session's group (EPERM) — found on a live Twitch run |
| Stage A on speech uses speech only; OCR counts only while < 20 s old | A stale OCR hit re-triggered the judge every few seconds (found on LiveNOW FOX) |
| Trim words repeated across the 2 s window overlap before storing a row | Whisper segment boundaries differ between overlapping windows |
| Instant reply does no DNS; `is_safe_url` runs on the watcher thread | Brief: no network before the first reply |
| Plan failure ends the watch (one retry) | Brief; the second message says why |
| Judge aux task defaults `reasoning_effort: low` | Thinking models; users can set `none` |
| `requires_hermes: ">=0.23.0"` | Only release tested; older-release fallbacks (`getattr` / `hasattr`) are untested |
| Dev testing via stub OpenAI server + live harness | No keys touched, deterministic, near-zero LLM spend |

## Done

- [x] Spike 1a–1f (SPIKE.md). GO.
- [x] Demo assets + generator. Skeleton, doctor clean.
- [x] Full P0 pipeline; dev-CLI E2E on `demo://keynote` (speed 4 and real time): alert 6.5 s after the
      price, `/wakeme clip` 5.4 min later returns the right moment, catch-up, stop, no ffmpeg left.
- [x] find on demo, two YouTube VODs (captions) and a NASA MP4 (speech recognition).
- [x] Live harness: 3 YouTube Live news streams × 13.6 min, 0 false alerts, never `behind`.
- [x] Tests (56), validate, doctor. README, CHANGELOG, LICENSE, banner. Submission drafts.
- [x] P1: vision stage (`wakeme_vision`), auto-attach via injection, automation blueprint — code + unit
      coverage; vision and auto-attach not verified against a real model/gateway.

## Next

1. Live true-positive run (Trump mention on LiveNOW FOX; Twitch sub thanks) → TESTING.md latency budget.
2. Screenshots from real runs; README image paths.
3. Telegram E2E once the user connects Telegram and grants permission.
