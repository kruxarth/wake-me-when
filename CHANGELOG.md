# Changelog

## 0.1.0 — 2026-11-09

First release (Nous Research Hermes Agent Plugin Hackathon).

- `/wakeme <url> when <condition>` for YouTube Live, Twitch (streamlink pipe), HLS, RTSP, web media,
  local files (CLI/TUI/Desktop or `allowed_media_dirs`) and the bundled `demo://keynote` / `demo://launch`
  (`?speed=` for fast tests). Instant reply, then a watch plan; `--every [min]` for repeat alerts.
- Cascade: free fuzzy/phonetic cue matching and keyframe OCR → LLM judge (`wakeme_judge` aux task) that
  cites transcript rows → confirm/debounce (a second positive must come after newer speech; never re-fires on
  old evidence) → alert with quote, stream time and jump-in link.
- Optional vision question on changed keyframes (`wakeme_vision` aux task).
- Clips: evidence segments copied out of a rolling 5 s segment ring at fire time; MP4 cut on demand and
  delivered as native media from `/wakeme clip` and `wakeme_clip`.
- `/catchup` from rolling 10-minute summaries plus the unsummarised tail.
- `/wakeme find <url> <question>`: captions first (yt-dlp subtitles, demo VTT, sidecar files), local
  speech recognition fallback, keyword shortlist + one LLM rerank, ranked timestamps with deep links.
- `/watching`, `/wakeme stop <id|all>`, `/wakeme notit <id>` (negative examples), `/wakeme doctor` and
  `hermes wakeme doctor`.
- Tested end to end on a real Telegram gateway; fixed there: a post-alert crash (evidence index race), clips
  cut short when auto-attached, and `/wakeme stop` in the grace window reporting "stream ended".
- Seven agent tools, a bundled skill, a `pre_llm_call` hook that tells the agent which watches belong to
  the chat, and an automation blueprint ("watch a stream on a schedule").
- Robustness: one shared faster-whisper worker (Hermes's `stt-whisper` extra) for every watch; bounded
  queues and an audio ring that drops oldest audio instead of stalling ingest (`behind`); re-resolve and
  restart with backoff; per-watch hourly LLM budget with unconfirmed cue-only alerts when it runs out or
  the provider is down; process groups killed on stop/unload/exit; orphaned watches marked
  `stopped (gateway restart)` on load.
