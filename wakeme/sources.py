"""URL -> resolved source (yt-dlp / streamlink pipe / direct / demo://), URL safety, tool detection."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from . import vtt

PLUGIN_DIR = Path(__file__).resolve().parents[1]
DEMO_DIR = PLUGIN_DIR / "assets" / "demo"
DEMOS = ("keynote", "launch")

NET_WHITELIST = "https,http,tls,tcp,crypto"
RTSP_WHITELIST = "rtsp,rtp,udp,tcp,tls,https,http,crypto"
FILE_WHITELIST = "file"
PIPE_WHITELIST = "pipe"

LOCAL_SURFACES = {"cli", "tui", "desktop", "local"}

INSTALL_HINTS = {
    "ffmpeg": "install ffmpeg with your package manager (e.g. `sudo apt install ffmpeg`, `brew install ffmpeg`, `sudo pacman -S ffmpeg`)",
    "ffprobe": "ffprobe ships with ffmpeg (install ffmpeg)",
    "yt-dlp": "`uv tool install yt-dlp` (or `pipx install yt-dlp`) — needed for YouTube and most video sites",
    "streamlink": "`uv tool install streamlink` (or `pipx install streamlink`) — needed for Twitch",
    "tesseract": "install tesseract for on-screen text (e.g. `sudo apt install tesseract-ocr`, `brew install tesseract`)",
    "deno": "optional: install Deno (https://deno.com) so yt-dlp can read every YouTube format; node or bun also work",
}


class SourceError(Exception):
    """A user-facing reason the source can't be watched."""


def which(name: str) -> Optional[str]:
    return shutil.which(name)


def need(name: str) -> str:
    path = which(name)
    if not path:
        raise SourceError(f"`{name}` is not installed: {INSTALL_HINTS.get(name, '')}")
    return path


def js_runtime_args() -> list[str]:
    """yt-dlp only enables Deno by default; point it at node or bun when Deno is absent."""
    if which("deno"):
        return []
    for rt in ("node", "bun"):
        if which(rt):
            return ["--js-runtimes", rt]
    return []


@dataclass
class Source:
    url: str                      # what the user gave us
    kind: str                     # demo | youtube | twitch | hls | rtsp | http | file
    title: str = ""
    channel: str = ""
    is_live: bool = True
    inputs: list[str] = field(default_factory=list)   # ffmpeg -i values (1 or 2: video, audio)
    pipe_argv: Optional[list[str]] = None             # streamlink argv feeding ffmpeg stdin
    whitelist: str = NET_WHITELIST
    speed: float = 1.0
    captions: Optional[list] = None                   # vtt.Cue list (demo / sidecar)
    duration: Optional[float] = None
    video_id: str = ""                                # YouTube id for &t= links

    @property
    def paced(self) -> bool:
        """Finite media played as if live (demo, local file, VOD) must be read in real time."""
        return self.kind in {"demo", "file"} or not self.is_live

    @property
    def label(self) -> str:
        names = {"demo": "demo stream", "youtube": "YouTube Live" if self.is_live else "YouTube",
                 "twitch": "Twitch", "hls": "HLS", "rtsp": "RTSP", "http": "web media", "file": "local file"}
        return names.get(self.kind, self.kind)

    def link(self, pos: Optional[float] = None) -> str:
        """Jump-in link. ``&t=`` only for VODs (a live link always jumps to the live edge)."""
        if pos is None or self.is_live and self.kind != "demo":
            return self.url
        t = max(0, int(pos) - 3)
        if self.kind == "youtube" and self.video_id:
            return f"https://www.youtube.com/watch?v={self.video_id}&t={t}s"
        if self.kind == "twitch" and "/videos/" in self.url:
            h, rem = divmod(t, 3600)
            m, s = divmod(rem, 60)
            return f"{self.url.split('?')[0]}?t={h}h{m}m{s}s"
        return f"{self.url} (at {fmt_pos(t)})"


def fmt_pos(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ---- classification & safety ----------------------------------------------------------------

def classify(url: str) -> str:
    u = url.strip()
    if u.startswith("demo://"):
        return "demo"
    if u.startswith(("/", "~/", "./", "file://")) or (re.match(r"^[A-Za-z]:\\", u) is not None):
        return "file"
    p = urlparse(u)
    scheme = (p.scheme or "").lower()
    if scheme == "rtsp":
        return "rtsp"
    if scheme not in {"http", "https"}:
        return "unsupported"
    host = (p.hostname or "").lower()
    if host.endswith(("youtube.com", "youtu.be")):
        return "youtube"
    if host.endswith("twitch.tv"):
        return "twitch"
    if p.path.lower().endswith(".m3u8"):
        return "hls"
    return "http"


def _is_safe_http(url: str) -> bool:
    try:
        from tools.url_safety import is_safe_url
    except ImportError:  # outside Hermes (tests): conservative literal check only
        host = (urlparse(url).hostname or "").lower()
        return not (host in {"localhost", ""} or host.startswith(("127.", "10.", "192.168.", "169.254.")))
    return is_safe_url(url)


def local_path_allowed(path: Path, surface: str, allowed_dirs: list[str]) -> bool:
    if surface in LOCAL_SURFACES:
        return True
    real = path.resolve()
    for d in allowed_dirs:
        try:
            base = Path(os.path.expanduser(d)).resolve()
        except (OSError, RuntimeError):
            continue
        if real == base or base in real.parents:
            return True
    return False


def check_url(url: str, *, surface: str, allow_private: bool = False, allowed_dirs: list[str] = (),
              network: bool = True) -> str:
    """Validate a user-supplied source and return its kind. Raises SourceError with the reason.
    ``network=False`` skips the DNS-resolving private-address check (command handlers reply before
    any network I/O; the watcher thread runs the full check)."""
    kind = classify(url)
    if kind == "unsupported":
        raise SourceError("Only http(s), rtsp, demo:// links and local files are supported.")
    if kind == "demo":
        name = urlparse(url).netloc or url[len("demo://"):].split("?")[0]
        if name not in DEMOS:
            raise SourceError("Unknown demo. Try demo://keynote or demo://launch.")
        return kind
    if kind == "file":
        path = Path(os.path.expanduser(url[7:] if url.startswith("file://") else url))
        if not local_path_allowed(path, surface, list(allowed_dirs)):
            raise SourceError("Local files can only be watched from the Hermes CLI/TUI/Desktop, or from a "
                              "folder listed in the `allowed_media_dirs` setting.")
        if not path.is_file():
            raise SourceError(f"No such file: {path}")
        return kind
    if not network:
        return kind
    if kind in {"youtube", "twitch", "hls", "http"} and not allow_private and not _is_safe_http(url):
        raise SourceError("That address points at a private or local network. Enable the "
                          "`allow_private_network` setting to watch LAN cameras.")
    if kind == "rtsp" and not allow_private:
        host = urlparse(url).hostname or ""
        if not _is_safe_http(f"https://{host}/"):
            raise SourceError("That RTSP camera is on a private network. Enable the "
                              "`allow_private_network` setting to watch it.")
    return kind


# ---- resolution -----------------------------------------------------------------------------

def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise SourceError(f"{Path(argv[0]).name} timed out after {int(timeout)} s") from exc


def _short_err(stderr: str) -> str:
    lines = [l for l in (stderr or "").splitlines() if "ERROR" in l or "error" in l.lower()]
    msg = (lines[-1] if lines else (stderr or "").strip().splitlines()[-1:] or [""])
    msg = msg if isinstance(msg, str) else msg[0]
    msg = re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*)?", "", msg).strip()
    if "429" in msg or "Too Many Requests" in msg:
        return "YouTube refused the request (HTTP 429, rate limited). Try again in a few minutes."
    if "Sign in to confirm" in msg:
        return "YouTube asked to sign in to confirm you're not a bot; this plugin never reads browser cookies."
    return msg[:200] or "unknown error"


YT_FORMAT = "bv*[height<=480]+ba/b[height<=480]/b"


def ytdlp_info(url: str, fmt: Optional[str] = YT_FORMAT, timeout: float = 60) -> dict:
    argv = [need("yt-dlp"), "-J", "--no-playlist", "--no-warnings", *js_runtime_args()]
    if fmt:
        argv += ["-f", fmt]
    p = _run(argv + ["--", url], timeout)
    if p.returncode != 0:
        raise SourceError(f"yt-dlp: {_short_err(p.stderr)}")
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError as exc:
        raise SourceError("yt-dlp returned no metadata") from exc


def _demo(url: str) -> Source:
    p = urlparse(url)
    name = p.netloc or url[len("demo://"):].split("?")[0]
    try:
        speed = float(parse_qs(p.query).get("speed", ["1"])[0])
    except ValueError:
        speed = 1.0
    speed = min(max(speed, 0.5), 8.0)
    meta = json.loads((DEMO_DIR / "demos.json").read_text(encoding="utf-8")).get(name, {})
    media = DEMO_DIR / f"{name}.mp4"
    return Source(url=url, kind="demo", title=meta.get("title", name), channel="Wake Me When", is_live=True,
                  inputs=[str(media)], whitelist=FILE_WHITELIST, speed=speed,
                  captions=vtt.parse((DEMO_DIR / f"{name}.vtt").read_text(encoding="utf-8")),
                  duration=meta.get("duration"))


def _file(url: str) -> Source:
    path = Path(os.path.expanduser(url[7:] if url.startswith("file://") else url)).resolve()
    captions = None
    for ext in (".vtt", ".srt", ".en.vtt"):
        side = path.with_suffix(ext)
        if side.is_file():
            captions = vtt.parse(side.read_text(encoding="utf-8", errors="replace"))
            break
    return Source(url=str(path), kind="file", title=path.name, is_live=False, inputs=[str(path)],
                  whitelist=FILE_WHITELIST, captions=captions, duration=probe_duration(str(path), FILE_WHITELIST))


def probe_duration(target: str, whitelist: str) -> Optional[float]:
    if not which("ffprobe"):
        return None
    try:
        p = subprocess.run([which("ffprobe"), "-v", "error", "-protocol_whitelist", whitelist,
                            "-show_entries", "format=duration", "-of", "csv=p=0", "-i", target],
                           capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        return float(p.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, TypeError):
        return None


def resolve(url: str, kind: str) -> Source:
    """Network-touching resolution. Runs on the watcher thread, never in a command handler."""
    need("ffmpeg")
    if kind == "demo":
        return _demo(url)
    if kind == "file":
        return _file(url)
    if kind == "youtube":
        info = ytdlp_info(url)
        fmts = info.get("requested_formats") or [info]
        inputs = [f["url"] for f in fmts if f.get("url")]
        if not inputs:
            raise SourceError("yt-dlp found no playable stream")
        if len(fmts) == 2 and fmts[0].get("vcodec") == "none":  # keep (video, audio) order
            inputs.reverse()
        return Source(url=url, kind="youtube", title=info.get("title") or url,
                      channel=info.get("channel") or info.get("uploader") or "",
                      is_live=bool(info.get("is_live")), inputs=inputs, video_id=info.get("id") or "",
                      duration=info.get("duration"))
    if kind == "twitch":
        sl = need("streamlink")
        p = _run([sl, "--json", "--", url], 45)
        try:
            meta = json.loads(p.stdout or "{}")
        except json.JSONDecodeError:
            meta = {}
        if not meta.get("streams"):
            raise SourceError(meta.get("error") or "That Twitch channel is offline (no streams).")
        md = meta.get("metadata") or {}
        return Source(url=url, kind="twitch", title=md.get("title") or url, channel=md.get("author") or "",
                      is_live="/videos/" not in url, inputs=["pipe:0"], whitelist=PIPE_WHITELIST,
                      pipe_argv=[sl, "-O", "--loglevel", "warning", "--", url, "480p,best"])
    if kind == "rtsp":
        return Source(url=url, kind="rtsp", title=urlparse(url).hostname or url, is_live=True, inputs=[url],
                      whitelist=RTSP_WHITELIST)
    # hls / http: direct media. A finite duration means a VOD that we play in real time.
    dur = probe_duration(url, NET_WHITELIST)
    return Source(url=url, kind=kind, title=Path(urlparse(url).path).name or url, is_live=dur is None,
                  inputs=[url], whitelist=NET_WHITELIST, duration=dur)
