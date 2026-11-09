"""Direct HLS and web-media links: streamlink fetches through the loopback guard, which checks every
connection, so a public playlist can't point segments, keys or redirects at a LAN host.

Local servers stand in for the internet: 127.0.0.1 plays the public host and 127.0.0.2 the LAN host
(the address check is patched to refuse only 127.0.0.2). Linux routes all of 127/8 to loopback.
"""

import base64
import gc
import http.server
import shutil
import socket
import subprocess
import threading
import time

import pytest

from conftest import FakeCtx, needs_ffmpeg
from wake_me_when.wakeme import asr, guard, ingest, sources, vod
from wake_me_when.wakeme.app import App
from wake_me_when.wakeme.sources import SourceError
from wake_me_when.wakeme.store import Store

PUBLIC, LAN = "127.0.0.1", "127.0.0.2"
needs_streamlink = pytest.mark.skipif(shutil.which("streamlink") is None, reason="streamlink not installed")


class Site:
    """A tiny HTTP server: ``routes[path] = (status, headers, body)``; records every request path."""

    def __init__(self, host: str):
        site = self
        self.routes: dict[str, tuple[int, dict, bytes]] = {}
        self.hits: list[str] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                site.hits.append(self.path)
                status, headers, body = site.routes.get(self.path, (404, {}, b"not found"))
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        try:
            self.server = http.server.ThreadingHTTPServer((host, 0), Handler)
        except OSError:
            pytest.skip(f"can't bind {host}")
        self.base = f"http://{host}:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def add(self, path: str, body: bytes = b"", status: int = 200, **headers):
        self.routes[path] = (status, {k.replace("_", "-"): v for k, v in headers.items()}, body)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def net(monkeypatch):
    for k in sources.PROXY_ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(sources, "_is_safe_http", lambda url: LAN not in url)
    public, lan = Site(PUBLIC), Site(LAN)
    yield public, lan
    public.close()
    lan.close()


def playlist(*entries: str, endlist: bool = False, extra: str = "") -> bytes:
    body = "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n#EXT-X-MEDIA-SEQUENCE:0\n" + extra
    body += "".join(f"#EXTINF:2.0,\n{e}\n" for e in entries)
    return (body + ("#EXT-X-ENDLIST\n" if endlist else "")).encode()


def run_pipe(src: sources.Source, until, timeout: float = 25) -> bytes:
    """Run the source's streamlink like ingest does, until ``until()`` or ``timeout``; returns its output."""
    p = subprocess.Popen(src.pipe_argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=sources.tool_env())
    out = bytearray()
    reader = threading.Thread(target=lambda: out.extend(p.stdout.read()), daemon=True)
    reader.start()
    end = time.time() + timeout
    while time.time() < end and p.poll() is None and not until():
        time.sleep(0.1)
    p.kill()
    p.wait()
    reader.join(5)
    return bytes(out)


@pytest.fixture(scope="module")
def hls_vod(tmp_path_factory):
    """A real 6 s HLS VOD (three 2 s MPEG-TS segments with a tone and a test picture)."""
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    d = tmp_path_factory.mktemp("hls")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                    "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=6", "-c:v", "mpeg2video",
                    "-c:a", "mp2", "-f", "hls", "-hls_time", "2", "-hls_list_size", "0", "-hls_playlist_type", "vod",
                    "-hls_segment_filename", str(d / "seg%d.ts"), str(d / "vod.m3u8")], check=True, timeout=60)
    return {p.name: p.read_bytes() for p in d.iterdir()}


# ---- refusals --------------------------------------------------------------------------------------

@needs_streamlink
@pytest.mark.parametrize("where", ["segment", "key", "variant"])
def test_public_playlist_pointing_at_a_lan_host_is_refused(net, where):
    public, lan = net
    lan.add("/secret.ts", b"G" * 188 * 10)
    lan.add("/key.bin", b"k" * 16)
    lan.add("/media.m3u8", playlist(f"{public.base}/a.ts"))
    public.add("/a.ts", b"G" * 188 * 10)
    if where == "segment":
        public.add("/live.m3u8", playlist(f"{lan.base}/secret.ts", f"{lan.base}/secret.ts", f"{lan.base}/secret.ts"))
    elif where == "key":
        public.add("/live.m3u8", playlist("a.ts", "a.ts", "a.ts",
                                          extra=f'#EXT-X-KEY:METHOD=AES-128,URI="{lan.base}/key.bin"\n'))
    else:   # a master playlist whose variant lives on the LAN host: refused while reading the playlist
        public.add("/live.m3u8", f"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1000\n{lan.base}/media.m3u8\n".encode())
        with pytest.raises(SourceError, match=f"private or local network address \\({LAN}\\)"):
            sources.resolve(f"{public.base}/live.m3u8", "hls")
        assert lan.hits == []
        return
    src = sources.resolve(f"{public.base}/live.m3u8", "hls")
    assert src.pipe_argv and src.inputs == ["pipe:0"] and src.whitelist == sources.PIPE_WHITELIST
    run_pipe(src, until=lambda: src.ticket.refused)
    assert lan.hits == []                                  # the LAN host never saw a request
    assert src.ticket.refused and LAN in src.ticket.refused[0]
    assert "private or local network" in src.refused() and "allow_private_network" in src.refused()


@needs_ffmpeg
def test_control_ffmpeg_alone_fetches_the_lan_segment(net):
    """Why the guard exists: given the URL itself, ffmpeg fetches a playlist's LAN entry without asking."""
    public, lan = net
    lan.add("/secret.ts", b"G" * 188)
    public.add("/live.m3u8", playlist(f"{lan.base}/secret.ts", endlist=True))
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "quiet", "-nostdin", "-protocol_whitelist",
                    sources.NET_WHITELIST, "-i", f"{public.base}/live.m3u8", "-f", "null", "-"], timeout=30)
    assert "/secret.ts" in lan.hits


@needs_streamlink
def test_a_cross_host_redirect_to_a_lan_host_is_refused(net):
    public, lan = net
    lan.add("/live.m3u8", playlist("x.ts"))
    lan.add("/x.ts", b"G" * 188)
    # the playlist itself redirects: the plugin's own playlist read refuses it before streamlink starts
    public.add("/r.m3u8", status=302, Location=f"{lan.base}/live.m3u8")
    with pytest.raises(SourceError, match="private or local network"):
        sources.resolve(f"{public.base}/r.m3u8", "hls")
    # a public playlist whose segment redirects: streamlink follows it through the guard, which refuses
    public.add("/live.m3u8", playlist("seg.ts", "seg.ts", "seg.ts"))
    public.add("/seg.ts", status=302, Location=f"{lan.base}/x.ts")
    src = sources.resolve(f"{public.base}/live.m3u8", "hls")
    run_pipe(src, until=lambda: src.ticket.refused)
    assert lan.hits == [] and src.ticket.refused
    assert "/seg.ts" in public.hits


def test_a_server_that_answers_differently_later_is_still_checked(net):
    """There is no check-then-use gap: the guard checks each connection when it is made."""
    public, lan = net
    public.add("/p.m3u8", playlist("a.ts", endlist=True))
    assert guard.probe(f"{public.base}/p.m3u8", "hls").live is False
    public.add("/p.m3u8", status=302, Location=f"{lan.base}/p.m3u8")      # now it points at the LAN
    with pytest.raises(guard.Blocked):
        guard.fetch(f"{public.base}/p.m3u8")
    assert lan.hits == []


def test_every_dns_answer_is_checked_and_the_checked_address_is_dialled(net, monkeypatch):
    public, lan = net
    port = int(public.base.rsplit(":", 1)[1])
    answers = {"mixed.test": [PUBLIC, LAN], "public.test": [PUBLIC]}
    monkeypatch.setattr(guard.socket, "getaddrinfo", lambda host, port, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in answers[host]])
    with pytest.raises(guard.Blocked, match="mixed.test -> 127.0.0.2"):
        guard.dial("mixed.test", port, "http")
    sock = guard.dial("public.test", port, "http")
    assert sock.getpeername()[0] == PUBLIC
    sock.close()
    with pytest.raises(guard.Blocked):
        guard.dial(LAN, port, "http")


def test_the_proxy_needs_a_live_ticket(net):
    public, _ = net
    public.add("/x", b"hello")
    g = guard.get()

    def ask(creds):
        s = socket.create_connection(("127.0.0.1", g.port), timeout=5)
        auth = f"Proxy-Authorization: Basic {base64.b64encode(creds.encode()).decode()}\r\n" if creds else ""
        s.sendall(f"GET {public.base}/x HTTP/1.1\r\nHost: {public.base[7:]}\r\n{auth}\r\n".encode())
        data = b""
        while chunk := s.recv(65536):
            data += chunk
        s.close()
        return data

    t = g.ticket()
    ok = ask(f"{t.user}:{t.password}")
    assert ok.startswith(b"HTTP/1.0 200") or ok.startswith(b"HTTP/1.1 200")
    assert b"Connection: close" in ok and ok.endswith(b"hello")
    assert ask("").startswith(b"HTTP/1.1 407") and ask("wakeme:wrong").startswith(b"HTTP/1.1 407")
    creds = f"{t.user}:{t.password}"
    del t
    gc.collect()
    assert ask(creds).startswith(b"HTTP/1.1 407")        # a ticket nobody holds any more is dead


# ---- what still works -------------------------------------------------------------------------------

@needs_ffmpeg
@needs_streamlink
def test_a_same_host_playlist_plays_through_the_guard(net, hls_vod, tmp_path):
    public, lan = net
    for name, body in hls_vod.items():
        public.add(f"/v/{name}", body)
    src = sources.resolve(f"{public.base}/v/vod.m3u8", "hls")
    assert src.is_live is False and src.duration == pytest.approx(6, abs=0.5)   # from #EXT-X-ENDLIST, no ffprobe
    assert src.paced and "--http-proxy" in src.pipe_argv
    argv = ingest.build_ffmpeg_argv("ffmpeg", src, tmp_path, frames=False)
    assert argv[argv.index("-protocol_whitelist") + 1] == "pipe" and "-reconnect" not in argv
    ring = ingest.AudioRing()
    ing = ingest.Ingest(src, tmp_path / "work", frames=False, ring=ring)
    ing.start()
    try:
        assert ing.eof.wait(30)
    finally:
        ing.stop()
    assert ring.end_pos == pytest.approx(6, abs=1)      # all six seconds of audio arrived, read in real time
    assert {"/v/vod.m3u8", "/v/seg0.ts", "/v/seg2.ts"} <= set(public.hits)
    assert lan.hits == [] and src.refused() is None


@needs_ffmpeg
@needs_streamlink
def test_find_mode_reads_a_guarded_vod(net, hls_vod, monkeypatch):
    public, _ = net
    for name, body in hls_vod.items():
        public.add(f"/v/{name}", body)
    seen = {}
    monkeypatch.setattr(asr, "ready", lambda: True)
    monkeypatch.setattr(asr, "transcribe_file_pcm", lambda pcm, lang: seen.setdefault("pcm", pcm) and [])
    src, cues, how = vod.transcript_for(f"{public.base}/v/vod.m3u8", "hls", None)
    assert how == "speech recognition" and not src.is_live
    assert len(seen["pcm"]) == pytest.approx(6 * 16000 * 2, rel=0.1)   # 6 s of 16 kHz mono PCM via the pipe


def test_vod_and_live_detection_from_the_playlist(net):
    public, _ = net
    public.add("/master.m3u8", b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nlow/index.m3u8\n"
                               b"#EXT-X-STREAM-INF:BANDWIDTH=2\nhigh/index.m3u8\n")
    public.add("/low/index.m3u8", playlist("a.ts", "b.ts", endlist=True))
    public.add("/event.m3u8", playlist("a.ts", extra="#EXT-X-PLAYLIST-TYPE:EVENT\n"))
    public.add("/clip.mp4", b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 5000, Content_Type="video/mp4")
    public.add("/stream.m3u8.txt", playlist("a.ts"), Content_Type="application/vnd.apple.mpegurl")
    m = guard.probe(f"{public.base}/master.m3u8", "hls")
    assert (m.hls, m.live, m.duration) == (True, False, 4.0)
    assert guard.probe(f"{public.base}/event.m3u8", "hls").live is True
    f = guard.probe(f"{public.base}/clip.mp4", "http")
    assert (f.hls, f.live) == (False, False)                    # a sized body is a file, read in real time
    assert guard.probe(f"{public.base}/stream.m3u8.txt", "http").hls is True   # a playlist without .m3u8
    assert sources.is_recording(f"{public.base}/master.m3u8", "hls") is True
    assert sources.is_recording(f"{public.base}/event.m3u8", "hls") is False


def test_an_mp4_with_its_index_at_the_end_gets_a_clear_reason(net, monkeypatch):
    public, _ = net
    monkeypatch.setattr(sources, "need", lambda name: name)
    ftyp = (24).to_bytes(4, "big") + b"ftypisom" + b"\x00" * 12
    public.add("/end.mp4", ftyp + (8).to_bytes(4, "big") + b"free" + (5000).to_bytes(4, "big") + b"mdat" + b"\x00" * 4992)
    public.add("/front.mp4", ftyp + (40).to_bytes(4, "big") + b"moov" + b"\x00" * 32)
    with pytest.raises(SourceError, match="index at the end"):
        sources.resolve(f"{public.base}/end.mp4", "http")
    assert sources.resolve(f"{public.base}/front.mp4", "http").pipe_argv[-2].startswith("httpstream://")


def test_the_guard_is_skipped_with_allow_private_network_or_a_user_proxy(net, monkeypatch):
    public, _ = net
    monkeypatch.setattr(sources, "need", lambda name: name)
    monkeypatch.setattr(sources, "probe_duration", lambda url, wl: None)
    direct = sources.resolve(f"{public.base}/live.m3u8", "hls", allow_private=True)
    assert direct.inputs == [f"{public.base}/live.m3u8"] and direct.pipe_argv is None
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:3128")   # the user's proxy resolves names itself
    assert sources.guarded(False) is False
    assert sources.resolve(f"{public.base}/live.m3u8", "hls").pipe_argv is None


def test_without_streamlink_a_direct_link_is_refused_not_opened_unchecked(net, monkeypatch):
    public, _ = net
    monkeypatch.setattr(sources, "which", lambda name: None if name == "streamlink" else shutil.which(name))
    with pytest.raises(SourceError, match="streamlink"):
        sources.resolve(f"{public.base}/live.m3u8", "hls")


@needs_ffmpeg
@needs_streamlink
def test_a_watch_stops_and_says_why_when_the_guard_refuses(net, monkeypatch):
    public, lan = net
    public.add("/live.m3u8", playlist(f"{lan.base}/s.ts", f"{lan.base}/s.ts", f"{lan.base}/s.ts"))
    monkeypatch.setattr(asr, "ready", lambda: True)
    monkeypatch.setattr(asr.WORKER, "start", lambda: None)
    ctx = FakeCtx({"stt_backend": "local"})
    app = App(ctx, Store(), {"wakeme_judge"})
    app.watch(f"{public.base}/live.m3u8 when they announce the price", {"session_key": "k", "platform": "cli"})
    end = time.time() + 40
    while time.time() < end and not any(t.startswith("❌") for t in ctx.texts()):
        time.sleep(0.2)
    stopped = next((t for t in ctx.texts() if t.startswith("❌")), "")
    assert stopped.startswith("❌ Watch #1 stopped: the stream asked for an address on a private or local network")
    assert lan.hits == []
    app.shutdown()
