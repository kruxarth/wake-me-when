# PROGRESS — Wake Me When

Running log so another session can pick up. Newest state first in each section.

## HANDOFF (Nov 7, ~16:00 IST) — start here

**State: submittable, tagged `v0.1.0`.** 58 tests pass; `hermes plugins validate . --install-deps` passes
(security scan: safe); `hermes plugins doctor . --ci` OK (7 tools, 1 hook). TESTING.md has runs 1–8,
the latency budget (demo and live), find on 2 demos + 2 real recordings, and stop/disable/SIGTERM pgrep
evidence. README screenshots (`docs/screenshots/{alert,find}.png`) are rendered from verbatim run output
by `../spike/render_screens.py` (data in the session scratchpad; regenerate from `spike/live3.log` +
`spike/find_real.log`).

Fixed this session (all found on real runs):
- Debouncer: with `confirm: twice` a moment cited by every later window never confirmed (live4,
  9 × 0.90–0.95, no alert). Now a positive from a window with newer speech confirms. Run 8's live
  true positive (summit1g, "he talks about a quest he is doing") fired through this path.
- find: model answered `{"id": …}`, find dropped every hit. Prompt spells out the shape; `id` accepted.
- `/wakeme clip` reply lost the quote when auto-attach had already cut the clip.
- e2e test flake: `pgrep -f keynote.mp4` matched unrelated shells; now matches only `ffmpeg` processes.

**Only the user can do the rest** (see ../submission/NEXT_STEPS.md): Telegram E2E (TESTING.md steps;
then fill `<TELEGRAM_RESULT>` in PR_DESCRIPTION.md), push, fill SHA/links, open the PR, record the video,
submit the form. The question about installing into the real `~/.hermes` was never answered, so the
plugin has not been installed there.

Known limitations seen live: moments that match no cue phrase wait for the periodic judge (backs off to
4 min); YouTube bot-checks this IP after heavy use (the plugin reports it cleanly).

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
