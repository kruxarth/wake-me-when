# Changelog

## 0.1.2 — 2026-11-09

Closes the gap 0.1.1 disclosed: on direct HLS and web-media links, ffmpeg's own fetches weren't checked.

- Direct HLS and web-media links are fetched by `streamlink` through a network guard and piped into
  ffmpeg (`-protocol_whitelist pipe`), the way Twitch already worked. The guard is an HTTP proxy on
  `127.0.0.1` (random port, per-source random credentials). For every connection, including
  playlist variants, segments, AES keys and redirects, it resolves the host, refuses it if any
  address fails Hermes's `is_safe_url`, and dials the address it checked, so a playlist can't point
  ffmpeg at a LAN host and a DNS answer can't change between check and connect. A watch whose stream
  asks for a private address stops and says so; find mode refuses that recording.
- VOD or live comes from the playlist (`#EXT-X-ENDLIST`, summed `#EXTINF` durations) or a sized
  body, read through the same checks, instead of ffprobe on the URL.
- `streamlink` is now needed for these links unless `allow_private_network` is on or you set your
  own HTTP(S) proxy (then ffmpeg fetches them as before, and the proxy resolves names). Without it,
  the link is refused rather than opened unchecked.
- An MP4 whose index sits at the end of the file can't be streamed through a pipe; it now gets a
  clear reason (download it and watch the file) instead of an ffmpeg error.
- Tests with local servers: a public playlist's LAN segment, key or variant, a cross-host redirect
  to a LAN host and a mixed public/private DNS answer are refused; a same-host playlist plays and is
  searchable; the proxy needs a live ticket; a control test shows ffmpeg alone fetches the LAN entry.

## 0.1.1 — 2026-11-09

Catalog review fixes.

- External tools (ffmpeg, ffprobe, yt-dlp, streamlink, tesseract) run with a minimal environment
  instead of the Hermes one: `PATH`, `HOME`, `TMPDIR`, `LANG`/`LC_*`, `XDG_*`, plus the proxy
  variables, CA-bundle paths (`SSL_CERT_FILE`, `SSL_CERT_DIR`, `REQUESTS_CA_BUNDLE`) and
  `TESSDATA_PREFIX` when they are set. Provider keys and bot tokens no longer reach them. A test
  fails if any subprocess call in the plugin leaves out `env=`.
- The stream URLs yt-dlp and streamlink resolve a link to get the same private-network check as the
  link itself, for watches, the re-resolve after a stream URL expires, and find mode's audio URL.
- Only `youtube.com`, `youtu.be` and `twitch.tv` and their subdomains count as YouTube and Twitch
  links. A lookalike host (`notyoutube.com`) is now a plain web link, so it never reaches yt-dlp's
  generic extractor.

## 0.1.0 — 2026-11-08

First release (Nous Research Hermes Agent Plugin Hackathon).

- `/wakeme <url> when <condition>` for YouTube Live, Twitch (streamlink pipe), HLS, RTSP, web media,
  local files (CLI/TUI/Desktop or `allowed_media_dirs`) and the bundled `demo://keynote` / `demo://launch`
  (`?speed=` for fast tests). Instant reply, then a plain-words watch plan (cue phrases and a screen
  label, no regexes); "every time …" / "whenever …" / "every 10 minutes" in the condition for repeat alerts.
- Cascade: free fuzzy/phonetic cue matching and keyframe OCR → LLM judge (`wakeme_judge` aux task) that
  cites transcript rows → confirm/debounce (a second positive must come after newer speech; never re-fires on
  old evidence) → alert with quote, stream time and jump-in link.
- Optional vision question on changed keyframes (`wakeme_vision` aux task).
- Clips: evidence segments copied out of a rolling 5 s segment ring at fire time; MP4 cut on demand and
  delivered as native media from `/wakeme clip` and `wakeme_clip`.
- `/wakeme catchup` from rolling 10-minute summaries plus the unsummarised tail.
- `/wakeme <recording> <question>` (or `/wakeme find`): captions first (yt-dlp subtitles, demo VTT, sidecar files), local
  speech recognition fallback, keyword shortlist + one LLM rerank, ranked timestamps with deep links.
- One slash command: `/wakeme list`, `/wakeme catchup`, `/wakeme clip`, `/wakeme stop [id|all]`,
  `/wakeme wrong` (negative examples), `/wakeme doctor` and `hermes wakeme doctor`; ids are optional with
  one watch running. Alerts end with "Reply clip, not it or stop" and the agent maps those replies
  (`pre_llm_call` names the watch that just alerted). `/watching`, `/catchup`, `notit`, `--every` were
  folded into this before release; `notit`, `nope`, `status`, `watching` and `--every` still work.
- Tested end to end on a real Telegram gateway; fixed there: a post-alert crash (evidence index race), clips
  cut short when auto-attached, and `/wakeme stop` in the grace window reporting "stream ended".
- Seven agent tools, a bundled skill, a `pre_llm_call` hook that tells the agent which watches belong to
  the chat, and an automation blueprint ("watch a stream on a schedule").
- Robustness: one shared faster-whisper worker (Hermes's `stt-whisper` extra) for every watch; bounded
  queues and an audio ring that drops oldest audio instead of stalling ingest (`behind`); re-resolve and
  restart with backoff; per-watch hourly LLM budget with unconfirmed cue-only alerts when it runs out or
  the provider is down; process groups killed on stop/unload/exit; orphaned watches marked
  `stopped (gateway restart)` on load.
