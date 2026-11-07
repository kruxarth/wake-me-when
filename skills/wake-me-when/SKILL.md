---
name: wake-me-when
description: Watch live streams (keynotes, Twitch, launches, calls, cameras) for a spoken or on-screen moment and alert the user; search recordings for moments; hand over clips and catch-ups.
version: 0.1.0
author: kruxarth
license: MIT
metadata:
  hermes:
    tags: [streams, alerts, video, audio]
    category: automation
---

# Wake Me When

Use these tools when the user wants to be told when something happens in **live audio or video**
that has no feed: a keynote reaching pricing, an earnings call giving guidance, a Twitch streamer
reaching a boss, a council meeting reaching an agenda item, a rocket launch hitting T-minus 1 (or
being scrubbed), a nature cam showing an animal. Polling a web page or a score API is not this
plugin's job.

## Which tool

| User intent | Tool |
|---|---|
| "wake me / ping me / tell me when … <live link>" | `wakeme_watch(url, condition)`; "every time …" in the condition alerts on each occurrence |
| "where in this video do they talk about …", "find the part where …" (a recording) | `wakeme_find(url, question)` |
| "what's being watched?", "any alerts?" | `wakeme_list` |
| "clip", "send me the clip", "show me" after an alert | `wakeme_clip(watch_id)` |
| "not it", "wrong", "nope", "that's not it" after an alert | `wakeme_feedback(watch_id, note)`; the watch keeps going |
| "stop" after an alert | `wakeme_stop(watch_id)` |
| "catch me up", "what did I miss?" | `wakeme_catchup(watch_id, minutes?)` |
| "stop watching" | `wakeme_stop(watch_id or "all")` |

## Rules

- Pass the user's condition in their own words; the plugin plans the cues itself.
- `wakeme_watch` returns at once. The plan and the alert arrive later as separate messages from the
  plugin, not from you. Don't promise a time; say it will ping them here.
- Alerts are posted outside the conversation, so you won't see them in history. Each alert ends
  with "Reply clip, not it or stop", and while watches exist a "[Wake Me When plugin] Watches in this
  chat" note is added to the user's turn naming the watch that alerted most recently. A short reply
  like "clip", "not it" or "stop" right after an alert is about that watch: call the matching tool
  with its id. If the note names no recent alert and more than one watch could match, ask which one.
  Never guess an id.
- The one slash command is `/wakeme` (`/wakeme list`, `/wakeme catchup`, `/wakeme clip`,
  `/wakeme stop`); the id is optional when one watch is running.
- When `wakeme_clip` succeeds, the video is attached automatically; just say what it shows.
- For a first try without a real stream, suggest `demo://keynote` (a synthetic launch event where the
  price is announced) or `demo://launch` (a countdown with a hold).
- Local files can only be watched from the Hermes CLI/TUI/Desktop or an `allowed_media_dirs` folder.
- DRM-protected streams (Netflix, most paid sports) can't be watched.
