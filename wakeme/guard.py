"""Network guard for direct HLS and web-media links: every connection gets the private-network check.

ffmpeg fetches a playlist's variants, segments and keys and follows redirects by itself, with no hook
per request, so a public playlist could point it at a LAN host. For these links streamlink does the
fetching instead, with ``--http-proxy`` set to the loopback proxy here, and its output is piped into
ffmpeg (``-protocol_whitelist pipe``). The proxy resolves the host of every CONNECT and forwarded
request, refuses it if any address fails the check, and dials the address it checked, so a DNS
answer can't change between the check and the connection. The plugin's own small fetches (is a
playlist a VOD?) use :func:`fetch`, which dials the same way and checks every redirect hop.

The proxy listens on 127.0.0.1 on a random port and only accepts requests that carry the random
credentials of a live :class:`Ticket` (one per resolved source).
"""

from __future__ import annotations

import base64
import http.client
import logging
import secrets
import selectors
import socket
import socketserver
import ssl
import threading
import time
import weakref
from typing import Optional
from urllib.parse import urljoin, urlsplit

from . import sources

log = logging.getLogger(__name__)

HEAD_MAX = 64 * 1024        # request / response head size limit
IDLE_S = 120                # a tunnel with no traffic either way for this long is closed
CONNECT_TIMEOUT_S = 15
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) wake-me-when"
_HOP_HEADERS = {"connection", "keep-alive", "proxy-connection", "proxy-authorization", "proxy-authenticate", "te",
                "trailer", "upgrade"}


class Blocked(Exception):
    """The destination is (or resolves to) a private, local or otherwise refused address."""


def _bracket(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def dial(host: str, port: int, scheme: str, timeout: float = CONNECT_TIMEOUT_S) -> socket.socket:
    """Resolve ``host``, check the name and every address it resolves to, and connect to a checked address.
    Raises :class:`Blocked` when the name or any answer is refused, ``OSError`` when nothing answers."""
    host = (host or "").strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host or not 0 < port < 65536:
        raise Blocked(host or "empty host")
    if not sources._is_safe_http(f"{scheme}://{_bracket(host)}:{port}/"):
        raise Blocked(host)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise Blocked(f"{host} (DNS lookup failed)") from exc
    addrs = []
    for family, _, _, _, sockaddr in infos:
        ip = str(sockaddr[0]).split("%")[0]
        if not sources._is_safe_http(f"{scheme}://{_bracket(ip)}/"):   # every answer, not just the first
            raise Blocked(f"{host} -> {ip}" if ip != host else host)
        addrs.append((family, sockaddr))
    err: Optional[OSError] = None
    for family, sockaddr in addrs:
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            err = exc
            sock.close()
    raise err or OSError(f"no address for {host}")


# ---- tickets ------------------------------------------------------------------------------------

class Ticket:
    """Proxy credentials for one resolved source; collects what the proxy refused on its behalf. The
    proxy stops accepting them once the ticket is garbage-collected (a re-resolve makes a new one)."""

    def __init__(self, guard: "Guard"):
        self.user = "wakeme"
        self.password = secrets.token_urlsafe(18)
        self.proxy_url = f"http://{self.user}:{self.password}@127.0.0.1:{guard.port}"
        self.refused: list[str] = []
        self._lock = threading.Lock()

    def note(self, what: str) -> None:
        with self._lock:
            if len(self.refused) < 20:
                self.refused.append(what)

    def reason(self) -> Optional[str]:
        with self._lock:
            if not self.refused:
                return None
            first = self.refused[0]
        return (f"the stream asked for an address on a private or local network ({first}), so it was refused. "
                "Enable the `allow_private_network` setting to allow it.")


# ---- the loopback proxy ---------------------------------------------------------------------------

def _read_head(sock: socket.socket) -> tuple[bytes, bytes]:
    buf = b""
    while b"\r\n\r\n" not in buf:
        if len(buf) > HEAD_MAX:
            raise ValueError("head too large")
        chunk = sock.recv(65536)
        if not chunk:
            raise ValueError("closed before the head ended")
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    return head, rest


def _split_head(head: bytes) -> tuple[str, list[tuple[str, str]]]:
    lines = head.decode("latin-1").split("\r\n")
    headers = []
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers.append((name.strip(), value.strip()))
    return lines[0], headers


def _without_hop_headers(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    named = {v.strip().lower() for k, v in headers if k.lower() == "connection" for v in v.split(",")}
    return [(k, v) for k, v in headers if k.lower() not in _HOP_HEADERS and k.lower() not in named]


def _reply(sock: socket.socket, code: int, reason: str, extra: str = "") -> None:
    body = f"{code} {reason}\n".encode()
    try:
        sock.sendall(f"HTTP/1.1 {code} {reason}\r\n{extra}Content-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
                     f"Connection: close\r\n\r\n".encode() + body)
    except OSError:
        pass


def _relay(client: socket.socket, upstream: socket.socket, rewrite_response: bool) -> None:
    """Copy bytes both ways until either side closes or goes idle. With ``rewrite_response`` the
    response head gets ``Connection: close`` (one forwarded request per client connection)."""
    pending = b"" if rewrite_response else None
    sel = selectors.DefaultSelector()
    try:
        client.setblocking(True)
        upstream.setblocking(True)
        sel.register(client, selectors.EVENT_READ, upstream)
        sel.register(upstream, selectors.EVENT_READ, client)
        while True:
            events = sel.select(IDLE_S)
            if not events:
                return
            for key, _ in events:
                src, dst = key.fileobj, key.data
                data = src.recv(65536)
                if not data:
                    return
                if src is upstream and pending is not None:
                    pending += data
                    if b"\r\n\r\n" not in pending:
                        if len(pending) > HEAD_MAX:
                            return
                        continue
                    head, _, rest = pending.partition(b"\r\n\r\n")
                    status, headers = _split_head(head)
                    kept = _without_hop_headers(headers)
                    data = (status + "\r\n" + "".join(f"{k}: {v}\r\n" for k, v in kept)
                            + "Connection: close\r\n\r\n").encode("latin-1") + rest
                    pending = None
                dst.sendall(data)
    except OSError:
        return
    finally:
        sel.close()


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        guard: Guard = self.server.guard  # type: ignore[attr-defined]
        client: socket.socket = self.request
        client.settimeout(30)
        try:
            head, rest = _read_head(client)
            request_line, headers = _split_head(head)
            method, target, version = request_line.split(" ")
        except (OSError, ValueError):
            return _reply(client, 400, "Bad Request")
        ticket = guard.ticket_for(dict((k.lower(), v) for k, v in headers).get("proxy-authorization", ""))
        if ticket is None:
            return _reply(client, 407, "Proxy Authentication Required", 'Proxy-Authenticate: Basic realm="wakeme"\r\n')
        try:
            if method.upper() == "CONNECT":
                host, _, port = target.rpartition(":")
                scheme, port_n, out = "https", int(port), b""
            else:
                u = urlsplit(target)
                if u.scheme.lower() != "http" or not u.hostname:
                    return _reply(client, 400, "Bad Request")
                host, port_n, scheme = u.hostname, u.port or 80, "http"
                path = (u.path or "/") + (f"?{u.query}" if u.query else "")
                kept = _without_hop_headers(headers)
                out = (f"{method} {path} {version}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in kept)
                       + "Connection: close\r\n\r\n").encode("latin-1") + rest
        except ValueError:
            return _reply(client, 400, "Bad Request")
        try:
            upstream = dial(host, port_n, scheme)
        except Blocked as exc:
            log.warning("wake-me-when: refused a stream request to a private or local address: %s", exc)
            ticket.note(str(exc))
            return _reply(client, 403, "Forbidden")
        except OSError:
            return _reply(client, 502, "Bad Gateway")
        try:
            client.settimeout(None)
            upstream.settimeout(None)
            if method.upper() == "CONNECT":
                client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                out = rest
            if out:
                upstream.sendall(out)
            _relay(client, upstream, rewrite_response=method.upper() != "CONNECT")
        except OSError:
            pass
        finally:
            upstream.close()


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 64


class Guard:
    """The loopback proxy (one per Hermes process, started on first use)."""

    def __init__(self) -> None:
        self.server = _Server(("127.0.0.1", 0), _Handler)
        self.server.guard = self  # type: ignore[attr-defined]
        self.port = self.server.server_address[1]
        self._tickets: "weakref.WeakValueDictionary[str, Ticket]" = weakref.WeakValueDictionary()
        self._lock = threading.Lock()
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.5},
                                       name="wakeme-guard", daemon=True)
        self.thread.start()

    def ticket(self) -> Ticket:
        t = Ticket(self)
        with self._lock:
            self._tickets[f"{t.user}:{t.password}"] = t
        return t

    def ticket_for(self, proxy_authorization: str) -> Optional[Ticket]:
        scheme, _, value = proxy_authorization.partition(" ")
        if scheme.lower() != "basic":
            return None
        try:
            creds = base64.b64decode(value.strip(), validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        with self._lock:
            return self._tickets.get(creds)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


_GUARD: Optional[Guard] = None
_GUARD_LOCK = threading.Lock()


def get() -> Guard:
    global _GUARD
    with _GUARD_LOCK:
        if _GUARD is None:
            _GUARD = Guard()
        return _GUARD


def shutdown() -> None:
    global _GUARD
    with _GUARD_LOCK:
        g, _GUARD = _GUARD, None
    if g is not None:
        g.close()


# ---- the plugin's own small fetches ---------------------------------------------------------------

class _HTTP(http.client.HTTPConnection):
    def connect(self) -> None:
        self.sock = dial(self.host, self.port, "http", self.timeout)


class _HTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, timeout: float):
        self._ctx = ssl.create_default_context()
        super().__init__(host, port, timeout=timeout, context=self._ctx)

    def connect(self) -> None:
        self.sock = self._ctx.wrap_socket(dial(self.host, self.port, "https", self.timeout), server_hostname=self.host)


class Fetched:
    def __init__(self, url: str, status: int, headers: http.client.HTTPMessage, body: bytes, complete: bool):
        self.url, self.status, self.headers, self.body, self.complete = url, status, headers, body, complete


def fetch(url: str, *, max_bytes: int = 2 << 20, timeout: float = 20, max_redirects: int = 5) -> Fetched:
    """GET ``url`` with every connection checked (redirects included); at most ``max_bytes`` of the body,
    read within ``timeout`` seconds. Raises :class:`Blocked`, or ``OSError`` / ``http.client.HTTPException``."""
    deadline = time.monotonic() + timeout
    for _ in range(max_redirects + 1):
        u = urlsplit(url)
        scheme = u.scheme.lower()
        if scheme not in {"http", "https"} or not u.hostname:
            raise Blocked(f"unsupported redirect target {u.scheme}://")
        port = u.port or (443 if scheme == "https" else 80)
        conn = (_HTTPS if scheme == "https" else _HTTP)(u.hostname, port, timeout=timeout)
        try:
            conn.request("GET", (u.path or "/") + (f"?{u.query}" if u.query else ""),
                         headers={"User-Agent": USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity"})
            resp = conn.getresponse()
            location = resp.getheader("Location")
            if resp.status in {301, 302, 303, 307, 308} and location:
                url = urljoin(url, location.strip())
                continue
            body, complete = b"", False
            while len(body) < max_bytes and time.monotonic() < deadline:
                chunk = resp.read1(min(65536, max_bytes - len(body)))
                if not chunk:
                    complete = True
                    break
                body += chunk
            return Fetched(url, resp.status, resp.headers, body, complete)
        finally:
            conn.close()
    raise OSError("too many redirects")


# ---- what a direct link is -------------------------------------------------------------------------

class Probe:
    def __init__(self, hls: bool, live: bool, duration: Optional[float] = None, index_at_end: bool = False):
        self.hls, self.live, self.duration, self.index_at_end = hls, live, duration, index_at_end


def mp4_index_at_end(head: bytes) -> bool:
    """An MP4 whose ``moov`` index comes after the media data: ffmpeg can't read it from a pipe."""
    off = 0
    while off + 8 <= len(head):
        size, box = int.from_bytes(head[off:off + 4], "big"), head[off + 4:off + 8]
        if box == b"moov":
            return False
        if box == b"mdat":
            return True
        if size == 1 and off + 16 <= len(head):
            size = int.from_bytes(head[off + 8:off + 16], "big")
        if size < 8 or (off == 0 and box != b"ftyp"):
            return False
        off += size
    return False


def _playlist_lines(body: bytes) -> list[str]:
    return [l.strip() for l in body.decode("utf-8", errors="replace").lstrip("\ufeff").splitlines() if l.strip()]


def is_playlist(body: bytes) -> bool:
    return body.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"#EXTM3U")


def _media_playlist(lines: list[str]) -> tuple[bool, Optional[float]]:
    """(finished, total duration) of an HLS media playlist."""
    finished = "#EXT-X-ENDLIST" in lines or "#EXT-X-PLAYLIST-TYPE:VOD" in lines
    total = 0.0
    for l in lines:
        if l.startswith("#EXTINF:"):
            try:
                total += float(l[8:].split(",")[0])
            except ValueError:
                pass
    return finished, (total or None) if finished else None


def probe(url: str, kind: str) -> Probe:
    """Whether a direct link is an HLS playlist and whether it is live, without ffprobe touching it."""
    first = fetch(url, max_bytes=2 << 20 if kind == "hls" else 4096, timeout=20)
    if first.status >= 400:
        raise sources.SourceError(f"the server answered HTTP {first.status}")
    ctype = (first.headers.get("Content-Type") or "").lower()
    if is_playlist(first.body) or (kind == "hls" and "mpegurl" in ctype):
        lines = _playlist_lines(first.body)
        if not first.complete:
            raise sources.SourceError("that playlist is too large or too slow to read")
        variant = next((lines[i + 1] for i, l in enumerate(lines[:-1])
                        if l.startswith("#EXT-X-STREAM-INF") and not lines[i + 1].startswith("#")), None)
        if variant:
            media = fetch(urljoin(first.url, variant), max_bytes=4 << 20, timeout=20)
            if media.status >= 400 or not media.complete:
                return Probe(True, True)
            lines = _playlist_lines(media.body)
        finished, duration = _media_playlist(lines)
        return Probe(True, not finished, duration)
    # plain media: a body with a length is a file (played in real time), an open-ended body is live
    finite = first.headers.get("Content-Length") is not None and "chunked" not in (
        first.headers.get("Transfer-Encoding") or "").lower()
    return Probe(False, not finite, index_at_end=finite and mp4_index_at_end(first.body))
