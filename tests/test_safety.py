import json
import subprocess

import pytest

from wake_me_when.wakeme import ingest, sources
from wake_me_when.wakeme.sources import Source, SourceError


def _whitelists(argv):
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-protocol_whitelist"]


def _inputs(argv):
    return [argv[i + 1] for i, a in enumerate(argv) if a == "-i"]


@pytest.mark.parametrize("src,expected", [
    (Source(url="https://x.test/a.m3u8", kind="hls", inputs=["https://x.test/a.m3u8"]), sources.NET_WHITELIST),
    (Source(url="https://youtube.com/watch?v=x", kind="youtube", inputs=["https://v.test/v", "https://v.test/a"]),
     sources.NET_WHITELIST),
    (Source(url="rtsp://cam.test/s", kind="rtsp", inputs=["rtsp://cam.test/s"], whitelist=sources.RTSP_WHITELIST),
     sources.RTSP_WHITELIST),
    (Source(url="/tmp/a.mp4", kind="file", is_live=False, inputs=["/tmp/a.mp4"], whitelist=sources.FILE_WHITELIST),
     "file"),
    (Source(url="https://twitch.tv/x", kind="twitch", inputs=["pipe:0"], whitelist=sources.PIPE_WHITELIST,
            pipe_argv=["streamlink", "-O", "--", "https://twitch.tv/x", "best"]), "pipe"),
])
def test_every_ffmpeg_input_has_matching_whitelist(src, expected, tmp_path):
    argv = ingest.build_ffmpeg_argv("ffmpeg", src, tmp_path, frames=True)
    wl = _whitelists(argv)
    assert wl == [expected] * len(src.inputs)
    assert _inputs(argv) == src.inputs                    # URLs only ever appear as the value of -i
    for w in wl:                                          # network and file protocols never mix
        parts = set(w.split(","))
        assert not ("file" in parts and parts & {"http", "https", "tcp", "rtsp"})


def test_network_whitelist_never_contains_file():
    for w in (sources.NET_WHITELIST, sources.RTSP_WHITELIST, sources.PIPE_WHITELIST):
        assert "file" not in w.split(",")


def test_two_input_youtube_maps_audio_from_second_input(tmp_path):
    src = Source(url="u", kind="youtube", inputs=["https://v.test/v", "https://v.test/a"])
    argv = ingest.build_ffmpeg_argv("ffmpeg", src, tmp_path, frames=False)
    assert "1:a:0" in argv and "0:v:0?" in argv
    assert "-readrate" not in argv                        # live: never paced


def test_demo_and_files_are_paced(tmp_path):
    demo = sources.resolve("demo://keynote?speed=4", "demo") if sources.which("ffmpeg") else None
    if demo is None:
        pytest.skip("ffmpeg not installed")
    argv = ingest.build_ffmpeg_argv("ffmpeg", demo, tmp_path, frames=False)
    assert argv[argv.index("-readrate") + 1] == "4"
    assert _whitelists(argv) == ["file"]


def test_streamlink_and_ytdlp_put_double_dash_before_url(monkeypatch):
    seen = []

    def fake_run(argv, timeout):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout='{"streams": {"best": {}}, "metadata": {"title": "t"}}',
                                           stderr="")

    monkeypatch.setattr(sources, "_run", fake_run)
    monkeypatch.setattr(sources, "need", lambda name: name)
    src = sources.resolve("https://twitch.tv/-rf", "twitch")
    assert src.pipe_argv[src.pipe_argv.index("--") + 1] == "https://twitch.tv/-rf"
    sources.ytdlp_info("-oops")
    yt = seen[-1]
    assert yt[yt.index("--") + 1] == "-oops" and yt.index("--") == len(yt) - 2


def test_unsupported_schemes_rejected():
    for url in ("ftp://x/y", "javascript:alert(1)", "gopher://x", "data:video/mp4;base64,AA"):
        with pytest.raises(SourceError):
            sources.check_url(url, surface="cli")


def test_local_paths_refused_from_messaging_chats(tmp_path):
    f = tmp_path / "secret.mp4"
    f.write_bytes(b"x")
    with pytest.raises(SourceError, match="allowed_media_dirs"):
        sources.check_url(str(f), surface="telegram")
    with pytest.raises(SourceError, match="allowed_media_dirs"):
        sources.check_url(f"file://{f}", surface="")          # unknown surface = not local
    assert sources.check_url(str(f), surface="local") == "file"
    assert sources.check_url(str(f), surface="telegram", allowed_dirs=[str(tmp_path)]) == "file"
    # a symlink out of the allowed dir does not escape the check
    other = tmp_path.parent / "outside.mp4"
    other.write_bytes(b"x")
    link = tmp_path / "link.mp4"
    link.symlink_to(other)
    with pytest.raises(SourceError):
        sources.check_url(str(link), surface="telegram", allowed_dirs=[str(tmp_path)])


def test_private_addresses_need_the_setting(monkeypatch):
    monkeypatch.setattr(sources, "_is_safe_http", lambda url: "192.168." not in url)
    with pytest.raises(SourceError, match="allow_private_network"):
        sources.check_url("http://192.168.1.5/live.m3u8", surface="cli")
    with pytest.raises(SourceError, match="allow_private_network"):
        sources.check_url("rtsp://192.168.1.5/stream", surface="cli")
    assert sources.check_url("http://192.168.1.5/live.m3u8", surface="cli", allow_private=True) == "hls"
    # the instant reply path does no DNS work at all
    assert sources.check_url("http://192.168.1.5/live.m3u8", surface="cli", network=False) == "hls"


def test_unknown_demo_rejected():
    with pytest.raises(SourceError):
        sources.check_url("demo://../../etc/passwd", surface="cli")


def test_links_only_seek_on_vods():
    live = Source(url="https://www.youtube.com/@NASA/live", kind="youtube", is_live=True, video_id="abc")
    vod = Source(url="https://youtu.be/abc", kind="youtube", is_live=False, video_id="abc")
    assert live.link(125) == live.url
    assert vod.link(125) == "https://www.youtube.com/watch?v=abc&t=122s"


@pytest.mark.parametrize("url,kind", [
    ("https://www.youtube.com/watch?v=x", "youtube"), ("https://youtu.be/x", "youtube"),
    ("https://m.youtube.com/@NASA/live", "youtube"), ("https://www.twitch.tv/x", "twitch"),
    ("https://notyoutube.com/watch?v=x", "http"), ("https://youtube.com.evil.test/x", "http"),
    ("https://eviltwitch.tv/x.m3u8", "hls"), ("https://twitch.tv.evil.test/x", "http"),
])
def test_only_youtube_and_twitch_hosts_reach_their_resolvers(url, kind):
    assert sources.classify(url) == kind      # a lookalike host never reaches yt-dlp's generic extractor


def test_stream_urls_from_resolvers_get_the_private_network_check(monkeypatch):
    monkeypatch.setattr(sources, "need", lambda name: name)
    monkeypatch.setattr(sources, "_is_safe_http", lambda url: "10.0.0.5" not in url)
    yt = {"is_live": True, "requested_formats": [{"url": "https://r1.googlevideo.test/v", "vcodec": "avc1"},
                                                 {"url": "http://10.0.0.5/a", "vcodec": "none"}]}
    monkeypatch.setattr(sources, "ytdlp_info", lambda url, **kw: yt)
    with pytest.raises(SourceError, match="yt-dlp resolved that link to a private"):
        sources.resolve("https://www.youtube.com/watch?v=x", "youtube")
    assert len(sources.resolve("https://www.youtube.com/watch?v=x", "youtube", allow_private=True).inputs) == 2
    yt["requested_formats"][1]["url"] = "https://r1.googlevideo.test/a"
    assert sources.resolve("https://www.youtube.com/watch?v=x", "youtube").inputs[1].endswith("/a")

    twitch = {"streams": {"best": {"type": "hls", "url": "https://p.ttvnw.test/v1/playlist/x.m3u8",
                                   "master": "http://10.0.0.5/api/channel/hls/x.m3u8"}}, "metadata": {}}
    monkeypatch.setattr(sources, "_run", lambda argv, timeout: subprocess.CompletedProcess(
        argv, 0, stdout=json.dumps(twitch), stderr=""))
    with pytest.raises(SourceError, match="streamlink resolved"):
        sources.resolve("https://www.twitch.tv/x", "twitch")
    assert sources.resolve("https://www.twitch.tv/x", "twitch", allow_private=True).pipe_argv


def test_find_checks_the_audio_url_from_yt_dlp(monkeypatch, tmp_path):
    from wake_me_when.wakeme import vod
    monkeypatch.setattr(sources, "_is_safe_http", lambda url: "192.168." not in url)
    monkeypatch.setattr(sources, "ytdlp_info", lambda url, **kw: {"requested_formats": [{"url": "http://192.168.1.9/a"}]})
    with pytest.raises(vod.FindError, match="yt-dlp resolved that link to a private"):
        vod._youtube_audio_asr("https://youtu.be/x", {"duration": 60}, tmp_path, None)
