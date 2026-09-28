"""DropIto のダウンロードエンジン (yt-dlp ラッパー).

このモジュールは GUI に一切依存しない。ui.py からは次の API だけを使う:

* ``fetch_info`` / ``fetch_info_async`` … URL のタイトル・サムネイル等を取得
* ``DownloadTask``                      … 別スレッドでダウンロードを実行・キャンセル

コールバックはすべてワーカースレッドから呼ばれるので、GUI 側で
メインスレッドへ受け渡してから画面を更新すること。

単体テスト用に CLI としても動く::

    python downloader.py URL                # MP4 (最高画質)
    python downloader.py URL --mp3          # MP3 320kbps
    python downloader.py URL --info         # 情報取得のみ
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

import yt_dlp
from yt_dlp.postprocessor import PostProcessor
from yt_dlp.utils import DownloadCancelled

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

#: 出力ファイル名テンプレート (パスは DownloadRequest.output_dir)。
#: タイトルは 120 バイトで切る: 作業フォルダーや .part を足しても Windows の
#: パス長上限 (260 文字) に収まるように
OUTPUT_TEMPLATE = "%(title).120B [%(id)s].%(ext)s"

#: 「タイトル -ダウンロード元URL」形式。URL の / : ? は Windows で使えないので
#: yt-dlp が全角 (⧸ ： ？) に置き換える。パス長に収まるようタイトル 80 / URL 100 バイトで切る。
#: dropito_title は _FilenameFieldsPP が作る「末尾の #N を残して切ったタイトル」
TITLE_BYTES_WITH_URL = 80
OUTPUT_TEMPLATE_WITH_URL = "%(dropito_title,title).80B -%(webpage_url).100B.%(ext)s"

#: Cookie を読み込めるブラウザ (yt-dlp の --cookies-from-browser と同じ名前)
SUPPORTED_BROWSERS = ("firefox", "chrome", "edge", "brave", "opera", "vivaldi", "chromium")

#: YouTube の署名解読に使う JavaScript ランタイム (見つかったものを全部渡す)
JS_RUNTIMES = ("deno", "node", "bun")

#: サムネイルとして読み込む最大バイト数
MAX_THUMBNAIL_BYTES = 8 * 1024 * 1024

#: 進捗コールバックの最小間隔 (秒)
PROGRESS_INTERVAL = 0.1

#: 保存先の中に作る作業フォルダーの接頭辞 (途中ファイルはここに置き、最後に丸ごと消す)
TEMP_DIR_PREFIX = ".dropito-tmp-"

#: この秒数より古い作業フォルダーは異常終了の残骸とみなして削除する
STALE_TEMP_SECONDS = 24 * 3600

# postprocessor_hooks の "postprocessor" (pp_key: 先頭の "FFmpeg" は省かれる) → 表示文言
_PP_MESSAGES = {
    "Merger": "映像と音声を結合中…",
    "ExtractAudio": "MP3 に変換中…",
    "Metadata": "メタデータを書き込み中…",
    "EmbedThumbnail": "サムネイルを埋め込み中…",
    "ThumbnailsConvertor": "サムネイルを変換中…",
    "MoveFiles": "ファイルを保存中…",
    "Fixup": "ファイルを修正中…",
}

_ERROR_HINTS = (
    ("Sign in to confirm", "YouTube にボット判定されました。「Cookie」でログイン済みのブラウザを選ぶと回避できる場合があります。"),
    ("Unsupported URL", "このURLには対応していません。"),
    ("is not a valid URL", "URL の形式が正しくありません。"),
    ("Private video", "非公開の動画です。"),
    ("Video unavailable", "動画を利用できません (削除・非公開・地域制限など)。"),
    ("No video could be found", "このポストには動画が含まれていません。"),
    ("unable to obtain file audio codec", "この動画には音声トラックがないため、MP3 にできません。"),
    ("HTTP Error 429", "アクセスが集中しています (HTTP 429)。時間をおいて再試行してください。"),
    ("HTTP Error 403", "サーバーにアクセスを拒否されました (HTTP 403)。yt-dlp を最新版にする・deno を入れる・Cookie を指定する、のいずれかで直る場合があります。"),
    ("Failed to decrypt with DPAPI", "Chrome 系ブラウザの Cookie を復号できませんでした。Firefox の利用をおすすめします。"),
    ("Could not copy Chrome cookie database", "ブラウザの Cookie を読み込めませんでした。ブラウザを閉じてから再試行してください。"),
    ("cookies database", "ブラウザの Cookie が見つかりませんでした。ブラウザがインストールされているか確認してください。"),
    ("ffmpeg not found", "ffmpeg が見つかりません。README の手順で ffmpeg を用意してください。"),
    ("getaddrinfo failed", "ネットワークに接続できません。"),
    ("Failed to resolve", "ネットワークに接続できません。"),
    ("timed out", "通信がタイムアウトしました。"),
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
# X (旧Twitter) の「…/status/123/video/2」「…/photo/1」。番号付きだとその 1 本しか落ちない
_X_MEDIA_SUFFIX_RE = re.compile(
    r"^(https?://(?:(?:www|mobile)\.)?(?:x|twitter|fxtwitter|vxtwitter|fixupx|fixvx)\.com/"
    r"(?:i/web|i|[^/?#]+)/status(?:es)?/\d+)/(?:video|photo)/\d+/?",
    re.IGNORECASE,
)


class MediaType(str, Enum):
    """保存形式."""

    VIDEO = "mp4"
    AUDIO = "mp3"


class Stage(str, Enum):
    """進捗の段階."""

    PREPARING = "preparing"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"


class Outcome(str, Enum):
    """ダウンロードの最終結果."""

    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"


class UserCancelled(DownloadCancelled):
    """ユーザーがキャンセルボタンを押したときに yt-dlp の中から送出する."""

    msg = "ユーザーによってキャンセルされました"


# ---------------------------------------------------------------------------
# データ型
# ---------------------------------------------------------------------------


@dataclass
class DownloadRequest:
    """ダウンロード 1 件分の指定."""

    url: str
    output_dir: str
    media_type: MediaType = MediaType.VIDEO
    max_height: Optional[int] = None  # None なら最高画質
    audio_bitrate: int = 320  # MP3 のビットレート (kbps)
    prefer_h264: bool = False  # 編集ソフト向けに H.264 / AAC を優先
    embed_thumbnail: bool = True  # サムネイルとメタデータを埋め込む
    playlist: bool = False  # プレイリスト全体を保存
    cookies_browser: Optional[str] = None
    filename_with_url: bool = False  # ファイル名を「タイトル -ダウンロード元URL」にする
    all_post_videos: bool = True  # 1 つの投稿に動画が複数あれば全部保存する


@dataclass
class MediaInfo:
    """プレビュー表示用のメタデータ."""

    url: str
    title: str
    uploader: Optional[str] = None
    duration: Optional[float] = None
    extractor: str = ""
    webpage_url: str = ""
    thumbnail_url: Optional[str] = None
    thumbnail_data: Optional[bytes] = None
    is_playlist: bool = False
    entry_count: Optional[int] = None


@dataclass
class ProgressUpdate:
    """GUI に渡す進捗情報. percent は 0〜100 (不明なら None)."""

    stage: Stage
    percent: Optional[float] = None
    downloaded_bytes: Optional[int] = None
    total_bytes: Optional[int] = None
    speed: Optional[float] = None  # bytes/sec
    eta: Optional[float] = None  # 秒
    filename: Optional[str] = None
    stream: Optional[str] = None  # "video" / "audio" / None
    item_index: Optional[int] = None  # プレイリスト内の番号
    item_count: Optional[int] = None
    message: str = ""


@dataclass
class DownloadResult:
    outcome: Outcome
    files: list = field(default_factory=list)
    error: Optional[str] = None


ProgressCallback = Callable[[ProgressUpdate], None]
LogCallback = Callable[[str, str], None]  # (level, message)
FinishedCallback = Callable[[DownloadResult], None]


# ---------------------------------------------------------------------------
# 外部ツールの検出
# ---------------------------------------------------------------------------


def _search_dirs() -> list:
    """同梱バイナリを探すディレクトリ (優先順)."""
    dirs = []
    bundle_dir = getattr(sys, "_MEIPASS", None)  # PyInstaller の展開先
    if bundle_dir:
        dirs += [os.path.join(bundle_dir, "bin"), bundle_dir]
    if getattr(sys, "frozen", False):
        app_dir = os.path.dirname(sys.executable)
    else:
        app_dir = os.path.dirname(os.path.abspath(__file__))
    dirs += [os.path.join(app_dir, "bin"), app_dir, os.path.join(app_dir, "ffmpeg", "bin")]
    return dirs


def find_executable(name: str) -> Optional[str]:
    """アプリ同梱 → exe の隣 → PATH の順で実行ファイルを探す."""
    filename = f"{name}.exe" if os.name == "nt" else name
    for directory in _search_dirs():
        path = os.path.join(directory, filename)
        if os.path.isfile(path):
            return path
    return shutil.which(name)


def find_ffmpeg() -> Optional[str]:
    return find_executable("ffmpeg")


def detect_js_runtimes() -> dict:
    """利用可能な JS ランタイムを yt-dlp の js_runtimes 形式で返す."""
    runtimes = {}
    for name in JS_RUNTIMES:
        path = find_executable(name)
        if path:
            runtimes[name] = {"path": path}
    return runtimes


# ---------------------------------------------------------------------------
# yt-dlp オプション構築 (純粋関数なのでテストしやすい)
# ---------------------------------------------------------------------------


def is_valid_url(text: str) -> bool:
    return bool(_URL_RE.match(text.strip()))


def build_format_selection(request: DownloadRequest, has_ffmpeg: bool = True) -> tuple:
    """(format 文字列, format_sort リスト) を返す."""
    if request.media_type is MediaType.AUDIO:
        return "ba/b", []

    resolution = f"res:{request.max_height}" if request.max_height else "res"
    if request.prefer_h264:
        sort = ["vcodec:h264", resolution, "acodec:aac", "ext:mp4:m4a"]
    else:
        sort = [resolution, "ext:mp4:m4a"]

    # ffmpeg が無いと映像+音声を結合できないので、結合済みの単一ファイルを選ぶ
    fmt = "bv*+ba/b" if has_ffmpeg else "b[ext=mp4]/b"
    return fmt, sort


def expand_post_url(url: str) -> str:
    """投稿内の特定の動画を指す URL を、投稿全体の URL にする.

    X では動画を開いて共有すると「…/status/123/video/1」になり、そのままだと
    1 本しか保存されない。番号部分を外すと投稿内の動画がまとめて対象になる。
    """
    return _X_MEDIA_SUFFIX_RE.sub(r"\1", url.strip(), count=1)


def fit_title(title: str, index: Optional[int] = None, count: Optional[int] = None,
              limit: int = TITLE_BYTES_WITH_URL) -> str:
    """タイトルを limit バイト (UTF-8) に収める.

    複数動画の投稿やプレイリストでは末尾に " #番号" を必ず残す。同じ投稿の動画は
    URL が同じなので、切ったときに番号まで消えるとファイル名がかぶってしまうため。
    """
    suffix = ""
    if index and count and count > 1:
        suffix = f" #{index}"
        if title.endswith(suffix):
            title = title[: -len(suffix)]
    budget = max(limit - len(suffix.encode("utf-8")), 1)
    return title.encode("utf-8")[:budget].decode("utf-8", "ignore") + suffix


class _FilenameFieldsPP(PostProcessor):
    """ファイル名を決める直前 (when="video") に dropito_title を用意する."""

    def run(self, info):
        count = info.get("n_entries") or info.get("playlist_count")
        info["dropito_title"] = fit_title(info.get("title") or info.get("id") or "", info.get("playlist_index"), count)
        return [], info


def output_template(request: DownloadRequest) -> str:
    return OUTPUT_TEMPLATE_WITH_URL if request.filename_with_url else OUTPUT_TEMPLATE


def build_postprocessors(request: DownloadRequest) -> list:
    pps = []
    if request.media_type is MediaType.AUDIO:
        pps.append({
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": str(request.audio_bitrate),
        })
    if request.embed_thumbnail:
        pps.append({"key": "FFmpegMetadata", "add_chapters": True, "add_metadata": True})
        pps.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
    return pps


def build_ydl_options(
    request: DownloadRequest,
    ffmpeg_path: Optional[str] = None,
    js_runtimes: Optional[dict] = None,
) -> dict:
    """DownloadRequest から YoutubeDL に渡すオプション辞書を作る (hooks/logger は除く)."""
    fmt, sort = build_format_selection(request, has_ffmpeg=bool(ffmpeg_path))
    opts = {
        "format": fmt,
        "paths": {"home": request.output_dir},
        "outtmpl": {"default": output_template(request), "pl_thumbnail": ""},
        "noplaylist": not request.playlist,
        "postprocessors": build_postprocessors(request),
        "writethumbnail": request.embed_thumbnail,
        "quiet": True,
        "noprogress": True,
        "no_color": True,
        "socket_timeout": 20,
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
    }
    if sort:
        opts["format_sort"] = sort
    if request.media_type is MediaType.VIDEO and ffmpeg_path:
        opts["merge_output_format"] = "mp4"
    if request.media_type is MediaType.AUDIO:
        # 既に同名の .mp3 があれば再ダウンロードしない
        opts["final_ext"] = "mp3"
        # 変換元を yt-dlp に消させない。同じ動画を MP4 で保存済みだと、それを
        # 変換元として拾って削除してしまうため。keepvideo だと今回落とした変換元も
        # 保存先へ移動されるので、それは DownloadTask が完了後に消す
        opts["keepvideo"] = True
    if ffmpeg_path:
        opts["ffmpeg_location"] = ffmpeg_path
    if js_runtimes:
        opts["js_runtimes"] = js_runtimes
    if request.cookies_browser:
        opts["cookiesfrombrowser"] = (request.cookies_browser,)
    if request.playlist:
        # 1 本失敗してもプレイリストの残りは続ける
        opts["ignoreerrors"] = "only_download"
    return opts


# ---------------------------------------------------------------------------
# 表示用フォーマッタ
# ---------------------------------------------------------------------------


def format_bytes(num: Optional[float]) -> str:
    if num is None:
        return "--"
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num) < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} GB"  # pragma: no cover


def format_speed(bytes_per_sec: Optional[float]) -> str:
    return "--" if not bytes_per_sec else f"{format_bytes(bytes_per_sec)}/s"


def format_duration(seconds: Optional[float]) -> str:
    """90 → '1:30', 3700 → '1:01:40'."""
    if seconds is None or seconds < 0:
        return "--:--"
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def humanize_error(message: str) -> str:
    """yt-dlp のエラー文を日本語のヒント付きメッセージにする."""
    text = _ANSI_RE.sub("", str(message)).strip()
    text = re.sub(r"^ERROR:\s*", "", text)
    lowered = text.lower()
    for needle, hint in _ERROR_HINTS:
        if needle.lower() in lowered:
            return f"{hint}\n\n詳細: {text}"
    return text or "不明なエラーが発生しました。"


# ---------------------------------------------------------------------------
# yt-dlp 用ロガー
# ---------------------------------------------------------------------------


class _YdlLogger:
    """yt-dlp のログを (level, message) のコールバックへ流す."""

    def __init__(self, callback: Optional[LogCallback]):
        self._callback = callback

    def _emit(self, level: str, message: str) -> None:
        if self._callback:
            self._callback(level, _ANSI_RE.sub("", message))

    def debug(self, message: str) -> None:
        # yt-dlp は通常の info も debug() に流してくる。本物の debug だけ捨てる
        if not message.startswith("[debug] "):
            self._emit("info", message)

    def info(self, message: str) -> None:
        self._emit("info", message)

    def warning(self, message: str) -> None:
        self._emit("warning", message)

    def error(self, message: str) -> None:
        self._emit("error", message)


# ---------------------------------------------------------------------------
# メタデータ取得 (プレビュー)
# ---------------------------------------------------------------------------


def _pick_thumbnail(info: dict) -> Optional[str]:
    thumbs = [t for t in (info.get("thumbnails") or []) if t.get("url")]
    # 大きすぎず小さすぎないもの (プレビュー用) を優先
    sized = [t for t in thumbs if t.get("width") and 320 <= t["width"] <= 1280]
    if sized:
        return max(sized, key=lambda t: (t["width"], t.get("preference") or 0))["url"]
    if info.get("thumbnail"):
        return info["thumbnail"]
    return thumbs[-1]["url"] if thumbs else None


def fetch_info(
    url: str,
    cookies_browser: Optional[str] = None,
    playlist: bool = False,
    all_post_videos: bool = True,
    with_thumbnail: bool = True,
    logger: Optional[LogCallback] = None,
) -> MediaInfo:
    """URL のタイトル・サムネイル等を取得する (ダウンロードはしない).

    失敗時は yt-dlp の例外をそのまま送出する。
    """
    url = url.strip()
    if not is_valid_url(url):
        raise ValueError(f"{url!r} is not a valid URL")
    if all_post_videos:
        url = expand_post_url(url)

    opts = {
        "skip_download": True,
        "extract_flat": "in_playlist",
        "noplaylist": not playlist,
        "quiet": True,
        "no_color": True,
        "socket_timeout": 15,
        "logger": _YdlLogger(logger),
    }
    js_runtimes = detect_js_runtimes()
    if js_runtimes:
        opts["js_runtimes"] = js_runtimes
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        is_playlist = info.get("_type") == "playlist"
        entries = list(info.get("entries") or []) if is_playlist else []
        thumb_source = info
        if is_playlist and not info.get("thumbnails") and entries:
            thumb_source = entries[0] or {}

        media = MediaInfo(
            url=url,
            title=info.get("title") or info.get("id") or url,
            uploader=info.get("uploader") or info.get("channel") or info.get("uploader_id"),
            duration=info.get("duration"),
            extractor=info.get("extractor_key") or info.get("extractor") or "",
            webpage_url=info.get("webpage_url") or url,
            thumbnail_url=_pick_thumbnail(thumb_source),
            is_playlist=is_playlist,
            entry_count=info.get("playlist_count") or (len(entries) if is_playlist else None),
        )

        if with_thumbnail and media.thumbnail_url:
            try:
                with ydl.urlopen(media.thumbnail_url) as response:
                    media.thumbnail_data = response.read(MAX_THUMBNAIL_BYTES)
            except Exception as exc:  # サムネイルが取れなくてもプレビューは出す
                if logger:
                    logger("warning", f"サムネイルを取得できませんでした: {exc}")
    return media


def fetch_info_async(
    url: str,
    callback: Callable[[Optional[MediaInfo], Optional[str]], None],
    **kwargs,
) -> threading.Thread:
    """fetch_info を別スレッドで実行し callback(info, error_message) を呼ぶ."""

    def worker() -> None:
        try:
            info = fetch_info(url, **kwargs)
        except Exception as exc:
            callback(None, humanize_error(str(exc)))
        else:
            callback(info, None)

    thread = threading.Thread(target=worker, name="dropito-info", daemon=True)
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# ダウンロード本体
# ---------------------------------------------------------------------------


class DownloadTask:
    """1 件のダウンロードを管理する. start() で別スレッド実行、run() で同期実行."""

    def __init__(
        self,
        request: DownloadRequest,
        on_progress: Optional[ProgressCallback] = None,
        on_log: Optional[LogCallback] = None,
        on_finished: Optional[FinishedCallback] = None,
    ):
        self.request = request
        self._on_progress = on_progress
        self._on_log = on_log
        self._on_finished = on_finished
        self._cancel_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_emit = 0.0
        self._downloaded_names: set = set()  # 今回ダウンロードしたファイル名 (MP3 の変換元の掃除用)
        self.result: Optional[DownloadResult] = None

    # -- 公開 API ------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            raise RuntimeError("このタスクは既に実行中です")
        self._thread = threading.Thread(target=self._run_and_notify, name="dropito-download", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        """キャンセルを要求する. 次の進捗通知のタイミングで中断される."""
        self._cancel_event.set()

    @property
    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def run(self) -> DownloadResult:
        """ダウンロードを同期実行して結果を返す (例外は投げない)."""
        request = self.request
        try:
            os.makedirs(request.output_dir, exist_ok=True)
        except OSError as exc:
            return DownloadResult(Outcome.FAILED, error=f"保存先フォルダを作成できません: {exc}")

        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            if request.media_type is MediaType.AUDIO:
                return DownloadResult(Outcome.FAILED, error=humanize_error("ffmpeg not found (MP3 変換に必要です)"))
            self._log("warning", "ffmpeg が見つからないため、結合済みの単一ファイル (画質が下がる場合あり) を取得します")

        js_runtimes = detect_js_runtimes()
        if not js_runtimes:
            self._log("warning", "JavaScript ランタイム (deno / node) が見つかりません。YouTube の一部の画質が取得できない場合があります")

        opts = build_ydl_options(request, ffmpeg_path=ffmpeg, js_runtimes=js_runtimes)
        opts["logger"] = _YdlLogger(self._on_log)
        opts["progress_hooks"] = [self._progress_hook]
        opts["postprocessor_hooks"] = [self._postprocessor_hook]

        # .part / 結合前の映像・音声 / サムネイル / MP3 の変換元 はすべて作業フォルダーに置き、
        # 完成品だけが保存先へ移動される。成功・失敗・キャンセルのどれでも最後に丸ごと消す。
        # 保存先と同じドライブに作るので、完成品の移動は一瞬で終わる
        _remove_stale_temp_dirs(request.output_dir)
        existing_names = set(os.listdir(request.output_dir))
        try:
            temp_dir = tempfile.mkdtemp(prefix=TEMP_DIR_PREFIX, dir=request.output_dir)
        except OSError as exc:
            return DownloadResult(Outcome.FAILED, error=f"保存先フォルダに書き込めません: {exc}")
        _hide_path(temp_dir)
        opts["paths"]["temp"] = temp_dir

        url = request.url.strip()
        if request.all_post_videos and expand_post_url(url) != url:
            url = expand_post_url(url)
            self._log("info", f"投稿内のすべての動画を保存します: {url}")

        self._emit(ProgressUpdate(Stage.PREPARING, message="動画情報を取得中…"), force=True)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                if request.filename_with_url:
                    ydl.add_post_processor(_FilenameFieldsPP(), when="video")
                info = ydl.extract_info(url, download=True)
            if self.cancel_requested:
                raise UserCancelled()
        except Exception as exc:
            if self.cancel_requested or isinstance(exc, DownloadCancelled):
                return DownloadResult(Outcome.CANCELLED)
            return DownloadResult(Outcome.FAILED, error=humanize_error(str(exc)))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

        files = self._collect_files(info)
        if request.media_type is MediaType.AUDIO:
            self._remove_conversion_sources(existing_names, files)
        if not files:
            return DownloadResult(Outcome.FAILED, error="ファイルを保存できませんでした。ログを確認してください。")
        return DownloadResult(Outcome.COMPLETED, files=files)

    # -- 内部処理 ------------------------------------------------------------

    def _run_and_notify(self) -> None:
        try:
            self.result = self.run()
        except Exception as exc:  # 念のため: スレッドを黙って死なせない
            self.result = DownloadResult(Outcome.FAILED, error=humanize_error(str(exc)))
        if self._on_finished:
            self._on_finished(self.result)

    def _log(self, level: str, message: str) -> None:
        if self._on_log:
            self._on_log(level, message)

    def _emit(self, update: ProgressUpdate, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_emit < PROGRESS_INTERVAL:
            return
        self._last_emit = now
        if self._on_progress:
            self._on_progress(update)

    @staticmethod
    def _playlist_position(info: dict) -> tuple:
        count = info.get("n_entries") or info.get("playlist_count")
        index = info.get("playlist_autonumber") or info.get("playlist_index")
        return (index, count) if count and count > 1 else (None, None)

    @staticmethod
    def _overall_bytes(info: dict, downloaded: Optional[int], total: Optional[int]) -> tuple:
        """映像+音声を別々に落とす場合、2 本合計での進捗に換算する."""
        formats = info.get("requested_formats") or []
        if len(formats) < 2 or downloaded is None:
            return downloaded, total, False
        sizes = [f.get("filesize") or f.get("filesize_approx") for f in formats]
        current = info.get("format_id")
        index = next((i for i, f in enumerate(formats) if f.get("format_id") == current), None)
        if index is None:
            return downloaded, total, False
        if total:
            sizes[index] = total
        if not all(sizes):
            return downloaded, total, False
        return sum(sizes[:index]) + downloaded, sum(sizes), True

    def _progress_hook(self, d: dict) -> None:
        if self.cancel_requested:
            raise UserCancelled()

        info = d.get("info_dict") or {}
        status = d.get("status")
        if status == "downloading" and d.get("filename"):
            self._downloaded_names.add(os.path.basename(d["filename"]))
        stream = None
        if len(info.get("requested_formats") or []) > 1:
            stream = "audio" if info.get("vcodec") in (None, "none") else "video"

        if status not in ("downloading", "finished"):
            return
        downloaded = d.get("downloaded_bytes")
        total = d.get("total_bytes") or d.get("total_bytes_estimate")
        if status == "finished":
            total = total or downloaded
            downloaded = total
        speed = d.get("speed")
        eta = d.get("eta")
        downloaded, total, combined = self._overall_bytes(info, downloaded, total)
        if combined and speed:
            eta = max(total - downloaded, 0) / speed

        percent = None
        if total and downloaded is not None:
            percent = min(downloaded / total * 100, 100.0)
        elif d.get("fragment_count"):
            percent = min((d.get("fragment_index") or 0) / d["fragment_count"] * 100, 100.0)
        elif status == "finished":
            percent = 100.0

        index, count = self._playlist_position(info)
        self._emit(ProgressUpdate(
            Stage.DOWNLOADING,
            percent=percent,
            downloaded_bytes=downloaded,
            total_bytes=total,
            speed=speed if status == "downloading" else None,
            eta=eta if status == "downloading" else None,
            filename=d.get("filename"),
            stream=stream,
            item_index=index,
            item_count=count,
            message="ダウンロード完了" if status == "finished" else "",
        ), force=status == "finished")

    def _postprocessor_hook(self, d: dict) -> None:
        if self.cancel_requested:
            raise UserCancelled()
        if d.get("status") != "started":
            return
        name = d.get("postprocessor") or ""
        message = next((m for key, m in _PP_MESSAGES.items() if name.startswith(key)), "後処理中…")
        index, count = self._playlist_position(d.get("info_dict") or {})
        self._emit(ProgressUpdate(Stage.PROCESSING, message=message, item_index=index, item_count=count), force=True)

    def _remove_conversion_sources(self, existing_names: set, keep: list) -> None:
        """MP3 に変換し終えた変換元 (今回ダウンロードして保存先へ移動されたもの) を消す.

        開始前から保存先にあったファイルと完成品には触らない。
        """
        keep_paths = {os.path.normcase(os.path.abspath(p)) for p in keep}
        for name in self._downloaded_names - existing_names:
            path = os.path.join(self.request.output_dir, name)
            if os.path.normcase(os.path.abspath(path)) in keep_paths or not os.path.isfile(path):
                continue
            try:
                os.remove(path)
            except OSError as exc:
                self._log("warning", f"変換元ファイルを削除できませんでした: {exc}")

    @staticmethod
    def _collect_files(info: Optional[dict]) -> list:
        if not info:
            return []
        entries = info.get("entries") if info.get("_type") == "playlist" else [info]
        files = []
        for entry in entries or []:
            for download in (entry or {}).get("requested_downloads") or []:
                path = download.get("filepath") or download.get("_filename")
                if path and os.path.exists(path) and path not in files:
                    files.append(path)
        return files


def _hide_path(path: str) -> None:
    """Windows では作業フォルダーを隠しフォルダーにする."""
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetFileAttributesW(str(path), 0x02)  # FILE_ATTRIBUTE_HIDDEN
        except Exception:
            pass


def _remove_stale_temp_dirs(output_dir: str) -> None:
    """異常終了などで残った古い作業フォルダーを掃除する."""
    try:
        names = os.listdir(output_dir)
    except OSError:
        return
    now = time.time()
    for name in names:
        path = os.path.join(output_dir, name)
        if name.startswith(TEMP_DIR_PREFIX) and os.path.isdir(path):
            try:
                if now - os.path.getmtime(path) > STALE_TEMP_SECONDS:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# CLI (単体動作確認用)
# ---------------------------------------------------------------------------


def _cli(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="DropIto ダウンロードエンジン (CLI 動作確認用)")
    parser.add_argument("url")
    parser.add_argument("-o", "--output", default=os.getcwd(), help="保存先フォルダ")
    parser.add_argument("--mp3", action="store_true", help="MP3 で保存")
    parser.add_argument("--height", type=int, help="最大の縦解像度 (例: 1080)")
    parser.add_argument("--bitrate", type=int, default=320, help="MP3 のビットレート kbps")
    parser.add_argument("--h264", action="store_true", help="H.264/AAC を優先")
    parser.add_argument("--no-embed", action="store_true", help="サムネイル/メタデータを埋め込まない")
    parser.add_argument("--playlist", action="store_true", help="プレイリスト全体を保存")
    parser.add_argument("--cookies-from-browser", choices=SUPPORTED_BROWSERS)
    parser.add_argument("--url-in-name", action="store_true", help="ファイル名を「タイトル -ダウンロード元URL」にする")
    parser.add_argument("--single-video", action="store_true",
                        help="「…/video/2」のような URL で、投稿内のその 1 本だけを保存する")
    parser.add_argument("--info", action="store_true", help="情報を表示するだけ")
    args = parser.parse_args(argv)

    def log(level: str, message: str) -> None:
        if level != "info":
            print(f"\n[{level}] {message}", file=sys.stderr)

    if args.info:
        info = fetch_info(args.url, cookies_browser=args.cookies_from_browser, playlist=args.playlist,
                          all_post_videos=not args.single_video, logger=log)
        print(f"タイトル : {info.title}")
        print(f"投稿者   : {info.uploader}")
        print(f"長さ     : {format_duration(info.duration)}")
        print(f"サイト   : {info.extractor}")
        print(f"サムネ   : {info.thumbnail_url} ({len(info.thumbnail_data or b'')} bytes)")
        if info.is_playlist:
            print(f"件数     : {info.entry_count}")
        return 0

    def progress(update: ProgressUpdate) -> None:
        if update.stage is Stage.DOWNLOADING:
            percent = "--" if update.percent is None else f"{update.percent:5.1f}%"
            line = (f"{percent}  {format_bytes(update.downloaded_bytes)} / {format_bytes(update.total_bytes)}"
                    f"  {format_speed(update.speed)}  残り {format_duration(update.eta)}")
        else:
            line = update.message
        print(f"\r{line:<70}", end="", flush=True)

    request = DownloadRequest(
        url=args.url,
        output_dir=os.path.abspath(args.output),
        media_type=MediaType.AUDIO if args.mp3 else MediaType.VIDEO,
        max_height=args.height,
        audio_bitrate=args.bitrate,
        prefer_h264=args.h264,
        embed_thumbnail=not args.no_embed,
        playlist=args.playlist,
        cookies_browser=args.cookies_from_browser,
        filename_with_url=args.url_in_name,
        all_post_videos=not args.single_video,
    )
    result = DownloadTask(request, on_progress=progress, on_log=log).run()
    print()
    if result.outcome is Outcome.COMPLETED:
        for path in result.files:
            print(f"保存しました: {path}")
        return 0
    print(f"{result.outcome.value}: {result.error or ''}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(_cli())
