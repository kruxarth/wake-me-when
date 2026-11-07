"""Rolling catch-up summaries (one short LLM call per chunk of speech) and catch-up composition."""

from __future__ import annotations

from .sources import fmt_pos

SUMMARY_INSTRUCTIONS = """WAKEME_SUMMARY
Summarise this chunk of a live stream transcript in 2-4 short bullet points, each starting with the
stream time in [m:ss] or [h:mm:ss] taken from the row it describes. Facts only, no filler."""

CATCHUP_INSTRUCTIONS = """WAKEME_CATCHUP
The user just tuned in to a live stream. Using the rolling summaries and the latest raw transcript,
write a catch-up: at most 8 bullet points in time order, each starting with the stream time
([m:ss] / [h:mm:ss]). Lead with whatever matters most for the user's condition. Plain text."""

MIN_WORDS = 25


def rows_text(rows: list[tuple]) -> str:
    """rows: (id, pos, pos_end, wall, text)"""
    return "\n".join(f"[{fmt_pos(r[1])}] {r[4]}" for r in rows)


def word_count(rows: list[tuple]) -> int:
    return sum(len(r[4].split()) for r in rows)


def compose_input(condition: str, summaries: list[tuple], raw_rows: list[tuple], max_raw_chars: int = 5000) -> str:
    parts = [f"User's condition: {condition}"]
    if summaries:
        parts.append("Rolling summaries:")
        parts += [s[2] for s in summaries if s[2]]
    raw = rows_text(raw_rows)
    if len(raw) > max_raw_chars:
        raw = raw[-max_raw_chars:]
        raw = raw[raw.find("\n") + 1:]
    parts += ["Latest transcript:", raw or "(no speech yet)"]
    return "\n".join(parts)


def fallback(summaries: list[tuple], raw_rows: list[tuple], limit: int = 12) -> str:
    """No model available: stored summaries plus the latest transcript lines."""
    lines = [s[2] for s in summaries if s[2]]
    tail = raw_rows[-limit:]
    if tail:
        lines.append("Latest:")
        lines += [f"[{fmt_pos(r[1])}] {r[4]}" for r in tail]
    return "\n".join(lines) or "Nothing has been said yet."
