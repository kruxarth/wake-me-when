# PROGRESS — Wake Me When

Running log so another session can pick up. Newest state first in each section.

## Environment found (Nov 7, 13:00 IST)

- `hermes --version`: **v0.23.0** (`2bd378ed1b8d`, tag `v0.23.0`, released Nov 5), git install,
  Python 3.14.9 (Hermes-bundled CPython). `_ref/hermes-agent` is cloned at the same tag.
  (Pitfall: `git tag | sort -V` puts the old CalVer tags `v2026.9.x` above `v0.23.0`; use
  `gh release list`.)
- Model: `deepseek-flash` via DeepSeek. `hermes chat -q "reply with ok"` hung 120 s at 12:50 (gateway
  log: HTTP 500 from the provider), then worked at 12:56 (44 s total). The provider is flaky today.
- Gateway: running as a systemd user service. **`hermes status`: "Platforms: none configured"** —
  Telegram is not connected yet, so the gateway E2E (step 6) needs the user to set up Telegram.
- Permission question (install into real `~/.hermes`) asked at 12:51; no answer yet. Until then all
  work uses `HERMES_HOME=./.hermes-dev` with a local stub OpenAI server (`spike/stub_llm.py`).

## Where the docs differ from the brief

- `developer-guide/plugins/public-api.md` does not exist in v0.23.0. The list of importable modules
  is `_PUBLIC` in `hermes_cli/plugin_validate_private_imports.py` (warning-only lint). We import only:
  `plugins.plugin_storage`, `tools.url_safety:is_safe_url`, `agent.secret_scope` (unused so far),
  `pm` (`available`, `ensure_import`, `InstallError`), `gateway.session_context:get_session_env`.
- `tools.lazy_deps.ensure` / `LAZY_DEPS` are gone. Lazy installs go through `pm.ensure_import(extra)`,
  which only accepts Hermes's own extras. Hermes ships `stt-whisper` = `faster-whisper==1.2.1`, so the
  plugin uses that extra instead of declaring faster-whisper itself.
- `hermes chat` is a gateway client: plugins run in the gateway process for every surface. See SPIKE.md 1e.
- `ctx.post_message` does not deliver `MEDIA:`; posted messages are not in conversation history; the
  classic CLI does not display posts (SPIKE.md 1e). → alerts also queue for `/watching`, a
  `pre_llm_call` hook gives the agent watch context, clips go through command/tool replies.
- Catalog admission has 18 rules (catalog-submission.md mirrors README). Rule 11: secrets via
  `agent.secret_scope.get_secret`; rule 14: `requires_hermes` SemVer floor, also in `plugin.yaml`.

## Decisions

| Decision | Why |
|---|---|
| faster-whisper `base`, int8, 4 threads, one shared worker | RTF 0.055 → ~18 streams per model (SPIKE 1b) |
| Segments `-c:v copy -c:a aac` | Re-encoding cost 33–111 % of a core; copy keeps a watch far below one core |
| yt-dlp and streamlink: **user-installed CLIs**, detected at runtime with install hints | Not Hermes extras; keeps the plugin's dependency footprint zero; matches "no bundled binaries" |
| No `python_dependencies`; faster-whisper via `pm.ensure_import("stt-whisper")` | Hermes's own pinned extra; documented public helper |
| Demo assets generated with edge-tts (generation-time only) | Original synthetic speech, 840 KB for both demos |
| Watches live in the gateway; db rows still carry owner pid + start token | All surfaces share the gateway; the token guards against stale rows after restarts |
| Local stub OpenAI server for dev E2E | No keys touched, deterministic, zero LLM spend |

## Done

- [x] Spike 1a–1f (SPIKE.md). GO.
- [x] Demo assets + generator (`scripts/make_demo_assets.py`, `assets/demo/`).

## Next

1. Skeleton: manifest, `register(ctx)`, `/wakeme` echo; `hermes plugins doctor` in `.hermes-dev`.
2. Core pipeline on `demo://keynote` end to end.
3. find, tests, validate, README, submission drafts.
