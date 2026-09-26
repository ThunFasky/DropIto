"""downloader.py のテスト.

    pytest                       # 全部 (ネットワークを使うテストを含む)
    pytest -m "not network"      # オフラインのユニットテストだけ

ネットワークテストは実際に X (旧Twitter) と汎用 MP4 URL からダウンロードする。
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import threading

import pytest

import downloader as dl
from downloader import DownloadRequest, DownloadTask, MediaType, Outcome, Stage

X_URL = "https://x.com/captainamerica/status/719944021058060289"  # 3 秒の動画付きポスト
YOUTUBE_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"  # Me at the zoo (19 秒)
DIRECT_SMALL = "https://test-videos.co.uk/vids/bigbuckbunny/mp4/h264/360/Big_Buck_Bunny_360_10s_1MB.mp4"
DIRECT_LARGE = "https://test-videos.co.uk/vids/bigbuckbunny/mp4/h264/1080/Big_Buck_Bunny_1080_10s_30MB.mp4"

needs_ffmpeg = pytest.mark.skipif(not dl.find_ffmpeg(), reason="ffmpeg がありません")


def ffprobe(path: str) -> dict:
    out = subprocess.run(
        [dl.find_executable("ffprobe") or "ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
        check=True, capture_output=True, text=True,
    ).stdout
    return json.loads(out)


def streams(path: str, codec_type: str) -> list:
    return [s for s in ffprobe(path)["streams"]
            if s["codec_type"] == codec_type and not s.get("disposition", {}).get("attached_pic")]


# ---------------------------------------------------------------------------
# オフライン: オプション構築
# ---------------------------------------------------------------------------


class TestOptions:
    def test_video_best_quality(self):
        fmt, sort = dl.build_format_selection(DownloadRequest("https://a.b/c", "."))
        assert fmt == "bv*+ba/b"
        assert sort == ["res", "ext:mp4:m4a"]

    def test_video_height_limit(self):
        _, sort = dl.build_format_selection(DownloadRequest("https://a.b/c", ".", max_height=720))
        assert sort[0] == "res:720"

    def test_video_prefer_h264(self):
        _, sort = dl.build_format_selection(DownloadRequest("https://a.b/c", ".", max_height=1080, prefer_h264=True))
        assert sort == ["vcodec:h264", "res:1080", "acodec:aac", "ext:mp4:m4a"]

    def test_video_without_ffmpeg_picks_single_file(self):
        fmt, _ = dl.build_format_selection(DownloadRequest("https://a.b/c", "."), has_ffmpeg=False)
        assert fmt == "b[ext=mp4]/b"
        opts = dl.build_ydl_options(DownloadRequest("https://a.b/c", "."), ffmpeg_path=None)
        assert "merge_output_format" not in opts and "ffmpeg_location" not in opts

    def test_video_options(self):
        req = DownloadRequest("https://a.b/c", "/out", embed_thumbnail=False)
        opts = dl.build_ydl_options(req, ffmpeg_path="/usr/bin/ffmpeg", js_runtimes={"deno": {"path": "/d"}})
        assert opts["merge_output_format"] == "mp4"
        assert opts["paths"] == {"home": "/out"}
        assert opts["outtmpl"]["default"] == dl.OUTPUT_TEMPLATE
        assert opts["noplaylist"] is True
        assert opts["postprocessors"] == []
        assert opts["js_runtimes"] == {"deno": {"path": "/d"}}
        assert "keepvideo" not in opts

    def test_audio_options(self):
        req = DownloadRequest("https://a.b/c", "/out", media_type=MediaType.AUDIO, audio_bitrate=192)
        opts = dl.build_ydl_options(req, ffmpeg_path="/usr/bin/ffmpeg")
        assert opts["format"] == "ba/b"
        assert "format_sort" not in opts and "merge_output_format" not in opts
        extract = opts["postprocessors"][0]
        assert extract == {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        assert [p["key"] for p in opts["postprocessors"]] == ["FFmpegExtractAudio", "FFmpegMetadata", "EmbedThumbnail"]
        assert opts["writethumbnail"] is True
        # 既存の MP4 を変換元として消さないための設定
        assert opts["keepvideo"] is True and opts["final_ext"] == "mp3"

    def test_filename_template(self):
        plain = dl.build_ydl_options(DownloadRequest("https://a.b/c", "."), ffmpeg_path="ffmpeg")
        with_url = dl.build_ydl_options(DownloadRequest("https://a.b/c", ".", filename_with_url=True), ffmpeg_path="ffmpeg")
        assert plain["outtmpl"]["default"] == "%(title).120B [%(id)s].%(ext)s"
        assert with_url["outtmpl"]["default"] == "%(title).80B -%(webpage_url).100B.%(ext)s"

    def test_filename_with_url_is_windows_safe(self):
        import yt_dlp

        req = DownloadRequest("https://a.b/c", "out", filename_with_url=True)
        opts = {**dl.build_ydl_options(req, ffmpeg_path="ffmpeg"), "windowsfilenames": True}
        info = {"id": "1", "title": "動画: テスト?", "ext": "mp4",
                "webpage_url": "https://www.youtube.com/watch?v=jNQXAC9IVRw"}
        with yt_dlp.YoutubeDL(opts) as ydl:
            name = os.path.basename(ydl.prepare_filename(info))
        assert name == "動画： テスト？ -https：⧸⧸www.youtube.com⧸watch？v=jNQXAC9IVRw.mp4"
        assert not set('/:?*"<>|\\') & set(name)

    def test_cookies_and_playlist(self):
        req = DownloadRequest("https://a.b/c", ".", cookies_browser="firefox", playlist=True)
        opts = dl.build_ydl_options(req, ffmpeg_path="ffmpeg")
        assert opts["cookiesfrombrowser"] == ("firefox",)
        assert opts["noplaylist"] is False
        assert opts["ignoreerrors"] == "only_download"

    def test_options_are_accepted_by_yt_dlp(self, tmp_path):
        import yt_dlp

        for media in MediaType:
            req = DownloadRequest("https://a.b/c", str(tmp_path), media_type=media, max_height=720, prefer_h264=True)
            with yt_dlp.YoutubeDL(dl.build_ydl_options(req, ffmpeg_path=dl.find_ffmpeg())) as ydl:
                assert ydl.params["format"]


# ---------------------------------------------------------------------------
# オフライン: ヘルパー
# ---------------------------------------------------------------------------


class TestHelpers:
    @pytest.mark.parametrize("value, expected", [
        (None, "--"), (512, "512 B"), (1536, "1.5 KB"), (5 * 1024 ** 2, "5.0 MB"), (3.25 * 1024 ** 3, "3.2 GB"),
    ])
    def test_format_bytes(self, value, expected):
        assert dl.format_bytes(value) == expected

    def test_format_speed(self):
        assert dl.format_speed(None) == "--"
        assert dl.format_speed(2 * 1024 ** 2) == "2.0 MB/s"

    @pytest.mark.parametrize("value, expected", [
        (None, "--:--"), (-1, "--:--"), (0, "0:00"), (59.6, "1:00"), (90, "1:30"), (3700, "1:01:40"),
    ])
    def test_format_duration(self, value, expected):
        assert dl.format_duration(value) == expected

    @pytest.mark.parametrize("text, ok", [
        ("https://x.com/a/status/1", True), ("http://youtu.be/abc", True), ("  https://a.b  ", True),
        ("x.com/a", False), ("ftp://a.b", False), ("", False), ("https://a b", False),
    ])
    def test_is_valid_url(self, text, ok):
        assert dl.is_valid_url(text) is ok

    def test_humanize_error_adds_hint(self):
        msg = dl.humanize_error("\x1b[0;31mERROR:\x1b[0m [youtube] abc: Sign in to confirm you’re not a bot")
        assert msg.startswith("YouTube にボット判定されました")
        assert "詳細: [youtube] abc: Sign in to confirm" in msg
        assert "\x1b" not in msg

    @pytest.mark.parametrize("raw, hint", [
        ("could not find firefox cookies database in C:\\x", "ブラウザの Cookie が見つかりませんでした"),
        ("Could not copy Chrome cookie database. See https://...", "ブラウザの Cookie を読み込めませんでした"),
        ("unable to download video data: HTTP Error 403: Forbidden", "サーバーにアクセスを拒否されました"),
        ("Postprocessing: WARNING: unable to obtain file audio codec with ffprobe", "この動画には音声トラックがない"),
    ])
    def test_humanize_error_cookie_and_http(self, raw, hint):
        assert dl.humanize_error(raw).startswith(hint)

    def test_humanize_error_passthrough(self):
        assert dl.humanize_error("ERROR: something odd") == "something odd"
        assert dl.humanize_error("") == "不明なエラーが発生しました。"

    def test_find_executable_prefers_bundled(self, tmp_path, monkeypatch):
        name = "dropito-fake-tool"
        tool = tmp_path / (name + (".exe" if os.name == "nt" else ""))
        tool.write_text("")
        monkeypatch.setattr(dl, "_search_dirs", lambda: [str(tmp_path)])
        assert dl.find_executable(name) == str(tool)
        monkeypatch.setattr(dl, "_search_dirs", lambda: [])
        assert dl.find_executable(name) is None


# ---------------------------------------------------------------------------
# オフライン: 進捗フック
# ---------------------------------------------------------------------------


class TestTempDir:
    def test_stale_temp_dirs_are_removed(self, tmp_path):
        stale = tmp_path / (dl.TEMP_DIR_PREFIX + "old")
        fresh = tmp_path / (dl.TEMP_DIR_PREFIX + "new")
        other = tmp_path / "keep-me"
        for d in (stale, fresh, other):
            d.mkdir()
        old = os.path.getmtime(stale) - dl.STALE_TEMP_SECONDS - 60
        os.utime(stale, (old, old))
        os.utime(other, (old, old))
        dl._remove_stale_temp_dirs(str(tmp_path))
        assert not stale.exists() and fresh.exists() and other.exists()

    def test_failed_download_leaves_nothing(self, tmp_path):
        # ネットワーク不要: 存在しないローカルファイルは即失敗する
        result = DownloadTask(DownloadRequest("file:///nonexistent/video.mp4", str(tmp_path))).run()
        assert result.outcome is Outcome.FAILED
        assert os.listdir(tmp_path) == []


class TestProgressHook:
    @pytest.fixture(autouse=True)
    def no_throttle(self, monkeypatch):
        monkeypatch.setattr(dl, "PROGRESS_INTERVAL", 0)

    @staticmethod
    def make_task(**kw):
        updates = []
        task = DownloadTask(DownloadRequest("https://a.b/c", ".", **kw), on_progress=updates.append)
        return task, updates

    def test_single_file_progress(self):
        task, updates = self.make_task()
        task._progress_hook({"status": "downloading", "downloaded_bytes": 250, "total_bytes": 1000,
                             "speed": 100.0, "eta": 7, "filename": "/o/v.mp4", "tmpfilename": "/o/v.mp4.part",
                             "info_dict": {"vcodec": "avc1"}})
        u = updates[-1]
        assert (u.stage, u.percent, u.speed, u.eta, u.stream) == (Stage.DOWNLOADING, 25.0, 100.0, 7, None)

    def test_video_and_audio_are_combined(self):
        task, updates = self.make_task()
        info = {"requested_formats": [{"format_id": "137", "filesize": 900, "vcodec": "avc1"},
                                      {"format_id": "140", "filesize": 100, "vcodec": "none"}]}
        video = {**info, "format_id": "137", "vcodec": "avc1"}
        audio = {**info, "format_id": "140", "vcodec": "none"}
        task._progress_hook({"status": "downloading", "downloaded_bytes": 450, "total_bytes": 900, "speed": 10.0,
                             "filename": "/o/v.f137.mp4", "info_dict": video})
        task._progress_hook({"status": "finished", "downloaded_bytes": 900, "total_bytes": 900,
                             "filename": "/o/v.f137.mp4", "info_dict": video})
        task._progress_hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100, "speed": 10.0,
                             "filename": "/o/v.f140.m4a", "info_dict": audio})
        assert [round(u.percent, 1) for u in updates] == [45.0, 90.0, 95.0]
        assert [u.stream for u in updates] == ["video", "video", "audio"]
        assert updates[-1].eta == pytest.approx(5.0)  # 残り 50 bytes / 10 B/s

    def test_unknown_size_uses_fragments(self):
        task, updates = self.make_task()
        task._progress_hook({"status": "downloading", "downloaded_bytes": 1234, "fragment_index": 3,
                             "fragment_count": 12, "info_dict": {}})
        assert updates[-1].percent == 25.0
        assert updates[-1].total_bytes is None

    def test_cancel_raises_inside_hooks(self):
        task, _ = self.make_task()
        task.cancel()
        with pytest.raises(dl.UserCancelled):
            task._progress_hook({"status": "downloading", "info_dict": {}})
        with pytest.raises(dl.UserCancelled):
            task._postprocessor_hook({"status": "started", "postprocessor": "Merger", "info_dict": {}})

    def test_postprocessor_messages(self):
        task, updates = self.make_task()
        for name in ("Merger", "ExtractAudio", "FixupM3u8", "SomethingNew"):
            task._postprocessor_hook({"status": "started", "postprocessor": name, "info_dict": {}})
        task._postprocessor_hook({"status": "finished", "postprocessor": "Merger", "info_dict": {}})
        assert [u.message for u in updates] == ["映像と音声を結合中…", "MP3 に変換中…", "ファイルを修正中…", "後処理中…"]
        assert all(u.stage is Stage.PROCESSING for u in updates)


# ---------------------------------------------------------------------------
# ネットワーク: 実際にダウンロードする
# ---------------------------------------------------------------------------


def run_task(request: DownloadRequest, cancel_after_first_progress: bool = False):
    updates, logs = [], []
    task = DownloadTask(request, on_log=lambda level, msg: logs.append((level, msg)))

    def on_progress(update):
        updates.append(update)
        if cancel_after_first_progress and update.stage is Stage.DOWNLOADING:
            task.cancel()

    task._on_progress = on_progress
    result = task.run()
    return result, updates, logs


@pytest.mark.network
class TestFetchInfo:
    def test_x_post(self):
        info = dl.fetch_info(X_URL)
        assert "Captain America" in info.title
        assert info.extractor == "Twitter"
        assert info.duration and 2 < info.duration < 5
        assert info.thumbnail_url and info.thumbnail_data
        from PIL import Image

        assert Image.open(io.BytesIO(info.thumbnail_data)).width > 100

    def test_invalid_url(self):
        with pytest.raises(ValueError):
            dl.fetch_info("not a url")

    def test_async_reports_error_on_worker_thread(self):
        done = threading.Event()
        got = {}

        def callback(info, error):
            got.update(info=info, error=error, thread=threading.current_thread())
            done.set()

        dl.fetch_info_async("https://example.com/", callback)
        assert done.wait(60)
        assert got["info"] is None and "対応していません" in got["error"]
        assert got["thread"] is not threading.main_thread()

    def test_youtube(self):
        try:
            info = dl.fetch_info(YOUTUBE_URL, with_thumbnail=False)
        except Exception as exc:  # クラウド環境の IP は YouTube にボット判定されやすい
            if "Sign in to confirm" in str(exc) or "429" in str(exc):
                pytest.skip(f"YouTube がこの環境の IP をブロック: {str(exc)[:120]}")
            raise
        assert info.title == "Me at the zoo"


@pytest.mark.network
@needs_ffmpeg
class TestDownload:
    def test_x_mp4(self, tmp_path):
        result, updates, _ = run_task(DownloadRequest(X_URL, str(tmp_path)))
        assert result.outcome is Outcome.COMPLETED, result.error
        (path,) = result.files
        assert path.endswith(".mp4") and os.path.getsize(path) > 100_000
        assert streams(path, "video") and streams(path, "audio")
        stages = {u.stage for u in updates}
        assert {Stage.PREPARING, Stage.DOWNLOADING, Stage.PROCESSING} <= stages
        assert any(u.percent == 100.0 for u in updates)
        # サムネイルが埋め込まれ、単体の画像ファイルは残らない
        assert any(s.get("disposition", {}).get("attached_pic") for s in ffprobe(path)["streams"])
        assert sorted(os.listdir(tmp_path)) == [os.path.basename(path)]

    def test_x_filename_with_source_url(self, tmp_path):
        req = DownloadRequest(X_URL, str(tmp_path), embed_thumbnail=False, filename_with_url=True)
        result, _, _ = run_task(req)
        assert result.outcome is Outcome.COMPLETED, result.error
        name = os.path.basename(result.files[0])
        assert name.startswith("Captain America - ")
        assert " -https" in name and "x.com⧸captainamerica⧸status⧸719944021058060289" in name
        assert name.endswith(".mp4")
        assert os.listdir(tmp_path) == [name]

    def test_x_mp3_leaves_only_mp3(self, tmp_path):
        result, _, _ = run_task(DownloadRequest(X_URL, str(tmp_path), media_type=MediaType.AUDIO))
        assert result.outcome is Outcome.COMPLETED, result.error
        (mp3,) = result.files
        # 変換元の動画や作業フォルダーが保存先に残らないこと (リグレッション)
        assert os.listdir(tmp_path) == [os.path.basename(mp3)]

    def test_x_mp3_320k_keeps_existing_mp4(self, tmp_path):
        mp4 = run_task(DownloadRequest(X_URL, str(tmp_path), embed_thumbnail=False))[0]
        assert mp4.outcome is Outcome.COMPLETED, mp4.error

        result, updates, _ = run_task(DownloadRequest(X_URL, str(tmp_path), media_type=MediaType.AUDIO))
        assert result.outcome is Outcome.COMPLETED, result.error
        (mp3,) = result.files
        assert mp3.endswith(".mp3")
        (audio,) = streams(mp3, "audio")
        assert audio["codec_name"] == "mp3"
        assert int(audio["bit_rate"]) == 320_000
        assert any(u.message == "MP3 に変換中…" for u in updates)
        # 先に保存した MP4 が変換元として消されていないこと (リグレッション)
        assert os.path.exists(mp4.files[0])
        assert sorted(os.listdir(tmp_path)) == sorted(os.path.basename(p) for p in (mp4.files[0], mp3))

    def test_direct_mp4_with_height_and_h264(self, tmp_path):
        req = DownloadRequest(DIRECT_SMALL, str(tmp_path), max_height=720, prefer_h264=True, embed_thumbnail=False)
        result, _, _ = run_task(req)
        assert result.outcome is Outcome.COMPLETED, result.error
        (video,) = streams(result.files[0], "video")
        assert video["codec_name"] == "h264" and video["height"] == 360

    def test_cancel_removes_partial_files(self, tmp_path):
        req = DownloadRequest(DIRECT_LARGE, str(tmp_path), embed_thumbnail=False)
        result, updates, logs = run_task(req, cancel_after_first_progress=True)
        assert result.outcome is Outcome.CANCELLED
        assert os.listdir(tmp_path) == []  # .part も作業フォルダーも残らない
        assert updates[-1].percent is not None and updates[-1].percent < 100

    def test_unsupported_url_fails_with_hint(self, tmp_path):
        result, _, _ = run_task(DownloadRequest("https://example.com/", str(tmp_path)))
        assert result.outcome is Outcome.FAILED
        assert result.error.startswith("このURLには対応していません")

    @pytest.mark.skipif(bool(os.environ.get("DROPITO_SKIP_YOUTUBE_DOWNLOAD")),
                        reason="DROPITO_SKIP_YOUTUBE_DOWNLOAD が設定されています")
    def test_youtube_mp4_720p(self, tmp_path):
        result, _, _ = run_task(DownloadRequest(YOUTUBE_URL, str(tmp_path), max_height=720, embed_thumbnail=False))
        assert result.outcome is Outcome.COMPLETED, result.error
        (video,) = streams(result.files[0], "video")
        assert video["height"] <= 720
        assert streams(result.files[0], "audio")

    def test_start_runs_on_worker_thread(self, tmp_path):
        finished = threading.Event()
        seen = {}

        def on_finished(result):
            seen.update(result=result, thread=threading.current_thread())
            finished.set()

        task = DownloadTask(DownloadRequest(DIRECT_SMALL, str(tmp_path), embed_thumbnail=False), on_finished=on_finished)
        task.start()
        assert task.is_running
        assert finished.wait(120)
        assert seen["result"].outcome is Outcome.COMPLETED
        assert seen["thread"] is not threading.main_thread()
        assert not task.is_running

