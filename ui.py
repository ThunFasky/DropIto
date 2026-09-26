"""DropIto の画面 (CustomTkinter).

ダウンロード処理はすべて downloader.py に任せ、このモジュールは表示と入力だけを扱う。
downloader のコールバックはワーカースレッドから呼ばれるため、直接ウィジェットを
触らずに queue へ積み、メインスレッドの after() ループで取り出して反映する。
"""

from __future__ import annotations

import io
import os
import queue
import subprocess
import sys
import time
import tkinter as tk
import tkinter.font as tkfont
import traceback
from tkinter import filedialog, messagebox
from typing import Optional

import customtkinter as ctk
import yt_dlp
from PIL import Image

from downloader import (
    DownloadRequest,
    DownloadResult,
    DownloadTask,
    MediaInfo,
    MediaType,
    Outcome,
    ProgressUpdate,
    Stage,
    detect_js_runtimes,
    fetch_info_async,
    find_ffmpeg,
    format_bytes,
    format_duration,
    format_speed,
    is_valid_url,
)
from settings import APP_NAME, Settings

# 表示ラベル → downloader に渡す値
VIDEO_QUALITIES = {
    "最高画質": 0,
    "2160p (4K)": 2160,
    "1440p (WQHD)": 1440,
    "1080p (フルHD)": 1080,
    "720p (HD)": 720,
    "480p": 480,
    "360p": 360,
}
AUDIO_QUALITIES = {
    "320kbps (高音質)": 320,
    "256kbps": 256,
    "192kbps (標準)": 192,
    "128kbps (軽量)": 128,
}
FORMAT_LABELS = {MediaType.VIDEO: "MP4 (動画)", MediaType.AUDIO: "MP3 (音声)"}
COOKIE_BROWSERS = {"使用しない": "", "Firefox": "firefox", "Chrome": "chrome", "Edge": "edge", "Brave": "brave"}
APPEARANCES = {"ダーク": "dark", "ライト": "light", "システム": "system"}

THUMB_W, THUMB_H = 192, 108
POLL_MS = 50
PREVIEW_DELAY_MS = 600
LOG_MAX_LINES = 500

MUTED = ("gray40", "gray62")
CARD = ("gray86", "gray17")
THUMB_BG = ("gray78", "gray24")
DANGER = ("#c62828", "#b3261e")
DANGER_HOVER = ("#a31f1f", "#8c1d18")
DISABLED_BUTTON = ("gray72", "gray30")
ERROR_TEXT = ("#c62828", "#ff6b6b")
SUCCESS_TEXT = ("#2e7d32", "#5fd068")


def resource_path(*parts: str) -> str:
    """ソース実行時と PyInstaller 実行時の両方で同梱ファイルのパスを返す."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, *parts)


def reveal_in_file_manager(path: str) -> None:
    """エクスプローラーでファイルを選択状態にして開く (フォルダならそのまま開く)."""
    if sys.platform == "win32":
        if os.path.isfile(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path] if os.path.isfile(path) else ["open", path])
    else:
        subprocess.Popen(["xdg-open", path if os.path.isdir(path) else os.path.dirname(path)])


class DropItoApp(ctk.CTk):
    def __init__(self) -> None:
        self.settings = Settings.load()
        ctk.set_appearance_mode(self.settings.appearance)
        ctk.set_default_color_theme("blue")
        super().__init__()

        self.title(f"{APP_NAME} - 動画・音声ダウンローダー")
        self.geometry("820x840")
        self.minsize(700, 760)
        self._set_window_icon()

        self._events: "queue.Queue[tuple]" = queue.Queue()
        self._task: Optional[DownloadTask] = None
        self._url_text = ""
        self._preview_job: Optional[str] = None
        self._preview_token = 0
        self._thumb_image: Optional[ctk.CTkImage] = None
        self._indeterminate = False
        self._video_quality = self._label_for(VIDEO_QUALITIES, self.settings.max_height, "最高画質")
        self._audio_quality = self._label_for(AUDIO_QUALITIES, self.settings.audio_bitrate, "320kbps (高音質)")

        family = self._pick_font_family()
        self.font_title = ctk.CTkFont(family=family, size=24, weight="bold")
        self.font_heading = ctk.CTkFont(family=family, size=15, weight="bold")
        self.font_body = ctk.CTkFont(family=family, size=13)
        self.font_small = ctk.CTkFont(family=family, size=12)
        self.font_button = ctk.CTkFont(family=family, size=15, weight="bold")
        self.font_mono = ctk.CTkFont(family="Consolas" if sys.platform == "win32" else "monospace", size=11)

        self._build_ui()
        self._apply_settings_to_ui()
        self._report_environment()

        self.url_entry.focus_set()
        self.after(POLL_MS, self._poll_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # 画面構築
    # ------------------------------------------------------------------

    def _pick_font_family(self) -> Optional[str]:
        available = set(tkfont.families(self))
        for name in ("Yu Gothic UI", "Meiryo UI", "BIZ UDPGothic", "Noto Sans CJK JP", "Noto Sans JP", "IPAexGothic", "IPAPGothic"):
            if name in available:
                return name
        return None  # CustomTkinter の既定フォント

    def _set_window_icon(self) -> None:
        try:
            ico = resource_path("assets", "icon.ico")
            png = resource_path("assets", "icon.png")
            if sys.platform == "win32" and os.path.exists(ico):
                self.iconbitmap(ico)
            elif os.path.exists(png):
                self._icon_photo = tk.PhotoImage(file=png)
                self.iconphoto(True, self._icon_photo)
        except tk.TclError:
            pass

    def _card(self, row: int, **grid) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self, corner_radius=12, fg_color=CARD)
        options = {"row": row, "column": 0, "sticky": "ew", "padx": 18, "pady": (0, 12)}
        options.update(grid)
        frame.grid(**options)
        return frame

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(5, weight=1)

        # --- ヘッダー ---
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=20, pady=(12, 10))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text=APP_NAME, font=self.font_title).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            header, text="YouTube・X (旧Twitter) などの動画を MP4 / MP3 で保存",
            font=self.font_small, text_color=MUTED,
        ).grid(row=1, column=0, sticky="w")
        self.appearance_menu = ctk.CTkOptionMenu(
            header, values=list(APPEARANCES), width=110, font=self.font_small,
            dropdown_font=self.font_small, command=self._on_appearance_change,
        )
        self.appearance_menu.grid(row=0, column=1, rowspan=2, sticky="e")

        # --- URL ---
        url_card = self._card(row=1)
        url_card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(url_card, text="動画の URL", font=self.font_heading).grid(
            row=0, column=0, columnspan=3, sticky="w", padx=16, pady=(10, 4))
        self.url_entry = ctk.CTkEntry(
            url_card, height=40, font=self.font_body,
            placeholder_text="https://www.youtube.com/watch?v=…  /  https://x.com/…/status/…",
        )
        self.url_entry.grid(row=1, column=0, sticky="ew", padx=(16, 8), pady=(0, 12))
        self.url_entry.bind("<Return>", lambda _e: self._start_download())
        self._attach_context_menu(self.url_entry)
        self.paste_btn = ctk.CTkButton(
            url_card, text="クリップボードから貼り付け", height=40, font=self.font_body,
            command=self._paste_from_clipboard,
        )
        self.paste_btn.grid(row=1, column=1, padx=(0, 8), pady=(0, 12))
        self.clear_btn = ctk.CTkButton(
            url_card, text="クリア", width=70, height=40, font=self.font_body,
            fg_color="transparent", border_width=1, text_color=("gray15", "gray85"),
            command=self._clear_url,
        )
        self.clear_btn.grid(row=1, column=2, padx=(0, 16), pady=(0, 12))

        # --- プレビュー ---
        preview = self._card(row=2)
        preview.grid_columnconfigure(1, weight=1)
        self.thumb_frame = ctk.CTkFrame(preview, width=THUMB_W, height=THUMB_H, corner_radius=8, fg_color=THUMB_BG)
        self.thumb_frame.grid(row=0, column=0, rowspan=3, padx=(12, 16), pady=12)
        self.thumb_frame.grid_propagate(False)
        self.thumb_label: Optional[ctk.CTkLabel] = None
        self._set_thumbnail(None)
        self.title_label = ctk.CTkLabel(
            preview, text="URL を入力するとプレビューを表示します", font=self.font_heading,
            anchor="w", justify="left", wraplength=440,
        )
        self.title_label.grid(row=0, column=1, sticky="sew", padx=(0, 16), pady=(12, 2))
        self.meta_label = ctk.CTkLabel(
            preview, text="", font=self.font_small, text_color=MUTED, anchor="w", justify="left", wraplength=440)
        self.meta_label.grid(row=1, column=1, sticky="new", padx=(0, 16))
        self.preview_status = ctk.CTkLabel(
            preview, text="", font=self.font_small, text_color=MUTED, anchor="w", justify="left", wraplength=440)
        self.preview_status.grid(row=2, column=1, sticky="new", padx=(0, 16), pady=(0, 10))
        preview.grid_rowconfigure((0, 2), weight=1)
        preview.bind("<Configure>", self._on_preview_resize)

        # --- 設定 ---
        opts = self._card(row=3)
        opts.grid_columnconfigure(1, weight=1)
        label_opts = {"font": self.font_body, "anchor": "w"}
        head = {"column": 0, "sticky": "w", "padx": (16, 12), "pady": 5}
        cell = {"column": 1, "sticky": "w", "padx": (0, 16), "pady": 5}

        ctk.CTkLabel(opts, text="形式", **label_opts).grid(row=0, **{**head, "pady": (12, 5)})
        format_row = ctk.CTkFrame(opts, fg_color="transparent")
        format_row.grid(row=0, **{**cell, "pady": (12, 5)})
        self.format_switch = ctk.CTkSegmentedButton(
            format_row, values=list(FORMAT_LABELS.values()), font=self.font_body, height=32,
            command=self._on_media_type_change,
        )
        self.format_switch.grid(row=0, column=0)
        ctk.CTkLabel(format_row, text="品質", font=self.font_body).grid(row=0, column=1, padx=(24, 10))
        self.quality_menu = ctk.CTkOptionMenu(
            format_row, values=list(VIDEO_QUALITIES), width=190, height=32, font=self.font_body,
            dropdown_font=self.font_body, command=self._on_quality_change,
        )
        self.quality_menu.grid(row=0, column=2)

        ctk.CTkLabel(opts, text="オプション", **label_opts).grid(row=1, **head)
        checks = ctk.CTkFrame(opts, fg_color="transparent")
        checks.grid(row=1, **cell)
        self.h264_var = tk.BooleanVar()
        self.embed_var = tk.BooleanVar()
        self.playlist_var = tk.BooleanVar()
        self.url_name_var = tk.BooleanVar()
        check_opts = {"font": self.font_small, "checkbox_width": 20, "checkbox_height": 20}
        self.h264_check = ctk.CTkCheckBox(
            checks, text="H.264 優先 (AviUtl など編集ソフト向け)", variable=self.h264_var, **check_opts)
        self.h264_check.grid(row=0, column=0, sticky="w", padx=(0, 20), pady=(0, 6))
        self.embed_check = ctk.CTkCheckBox(
            checks, text="サムネイル・メタデータを埋め込む", variable=self.embed_var, **check_opts)
        self.embed_check.grid(row=0, column=1, sticky="w", pady=(0, 6))
        self.playlist_check = ctk.CTkCheckBox(
            checks, text="プレイリスト全体をダウンロード", variable=self.playlist_var,
            command=lambda: self._schedule_preview(delay=0), **check_opts)
        self.playlist_check.grid(row=1, column=0, sticky="w")
        self.url_name_check = ctk.CTkCheckBox(
            checks, text="ファイル名にダウンロード元 URL を付ける", variable=self.url_name_var, **check_opts)
        self.url_name_check.grid(row=1, column=1, sticky="w")

        ctk.CTkLabel(opts, text="Cookie", **label_opts).grid(row=2, **head)
        cookie_row = ctk.CTkFrame(opts, fg_color="transparent")
        cookie_row.grid(row=2, **cell)
        self.cookie_menu = ctk.CTkOptionMenu(
            cookie_row, values=list(COOKIE_BROWSERS), width=140, height=30, font=self.font_body,
            dropdown_font=self.font_body, command=lambda _v: self._schedule_preview(delay=0),
        )
        self.cookie_menu.grid(row=0, column=0)
        ctk.CTkLabel(
            cookie_row, text="ボット判定・ログインが必要な動画用 (Firefox 推奨)",
            font=self.font_small, text_color=MUTED,
        ).grid(row=0, column=1, padx=(10, 0))

        ctk.CTkLabel(opts, text="保存先", **label_opts).grid(row=3, **{**head, "pady": (5, 12)})
        out_row = ctk.CTkFrame(opts, fg_color="transparent")
        out_row.grid(row=3, column=1, sticky="ew", padx=(0, 16), pady=(5, 12))
        out_row.grid_columnconfigure(0, weight=1)
        self.output_var = tk.StringVar()
        self.output_entry = ctk.CTkEntry(out_row, textvariable=self.output_var, height=32, font=self.font_body)
        self.output_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self._attach_context_menu(self.output_entry)
        self.browse_btn = ctk.CTkButton(
            out_row, text="フォルダーを選択…", width=130, height=32, font=self.font_body, command=self._browse_output)
        self.browse_btn.grid(row=0, column=1, padx=(0, 8))
        self.open_btn = ctk.CTkButton(
            out_row, text="開く", width=60, height=32, font=self.font_body,
            fg_color="transparent", border_width=1, text_color=("gray15", "gray85"),
            command=self._open_output_dir,
        )
        self.open_btn.grid(row=0, column=2)

        # --- 進捗 ---
        prog = self._card(row=4)
        prog.grid_columnconfigure(0, weight=1)
        self.status_label = ctk.CTkLabel(prog, text="待機中", font=self.font_heading, anchor="w")
        self.status_label.grid(row=0, column=0, sticky="w", padx=16, pady=(10, 4))
        self.percent_label = ctk.CTkLabel(prog, text="", font=self.font_heading, anchor="e")
        self.percent_label.grid(row=0, column=1, sticky="e", padx=16, pady=(10, 4))
        self.progress = ctk.CTkProgressBar(prog, height=14, corner_radius=7)
        self.progress.set(0)
        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew", padx=16)
        self.detail_label = ctk.CTkLabel(prog, text="", font=self.font_small, text_color=MUTED, anchor="w")
        self.detail_label.grid(row=2, column=0, columnspan=2, sticky="w", padx=16, pady=(6, 4))
        self._reset_progress_details()

        buttons = ctk.CTkFrame(prog, fg_color="transparent")
        buttons.grid(row=3, column=0, columnspan=2, sticky="ew", padx=16, pady=(2, 12))
        buttons.grid_columnconfigure(0, weight=1)
        self.download_btn = ctk.CTkButton(
            buttons, text="ダウンロード開始", height=44, font=self.font_button, command=self._start_download)
        self.download_btn.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.cancel_btn = ctk.CTkButton(
            buttons, text="キャンセル", width=150, height=44, font=self.font_button,
            fg_color=DISABLED_BUTTON, hover_color=DANGER_HOVER, state="disabled", command=self._cancel_download,
        )
        self.cancel_btn.grid(row=0, column=1)

        # --- ログ ---
        log_card = self._card(row=5, sticky="nsew", pady=(0, 14))
        log_card.grid_columnconfigure(0, weight=1)
        log_card.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(log_card, text="ログ", font=self.font_small, text_color=MUTED).grid(
            row=0, column=0, sticky="w", padx=16, pady=(6, 0))
        self.log_box = ctk.CTkTextbox(log_card, height=70, font=self.font_mono, wrap="word", corner_radius=8)
        self.log_box.grid(row=1, column=0, sticky="nsew", padx=12, pady=(2, 12))
        self.log_box.tag_config("warning", foreground="#d99a00")
        self.log_box.tag_config("error", foreground="#e5534b")
        self.log_box.configure(state="disabled")

    def _attach_context_menu(self, entry: ctk.CTkEntry) -> None:
        """右クリックで 切り取り/コピー/貼り付け を出す (Tk の Entry には標準で無い)."""
        menu = tk.Menu(self, tearoff=0)
        for label, sequence in (("切り取り", "<<Cut>>"), ("コピー", "<<Copy>>"), ("貼り付け", "<<Paste>>")):
            menu.add_command(label=label, command=lambda s=sequence: entry._entry.event_generate(s))
        menu.add_separator()
        menu.add_command(label="すべて選択", command=lambda: entry._entry.select_range(0, "end"))

        def popup(event: tk.Event) -> None:
            entry.focus_set()
            menu.tk_popup(event.x_root, event.y_root)

        entry.bind("<Button-3>", popup)

    # ------------------------------------------------------------------
    # 設定の反映/保存
    # ------------------------------------------------------------------

    @staticmethod
    def _label_for(mapping: dict, value, default: str) -> str:
        return next((label for label, v in mapping.items() if v == value), default)

    def _apply_settings_to_ui(self) -> None:
        s = self.settings
        media = MediaType.AUDIO if s.media_type == MediaType.AUDIO.value else MediaType.VIDEO
        self.format_switch.set(FORMAT_LABELS[media])
        self._on_media_type_change(FORMAT_LABELS[media])
        self.h264_var.set(s.prefer_h264)
        self.embed_var.set(s.embed_thumbnail)
        self.playlist_var.set(s.playlist)
        self.url_name_var.set(s.filename_with_url)
        self.cookie_menu.set(self._label_for(COOKIE_BROWSERS, s.cookies_browser, "使用しない"))
        self.appearance_menu.set(self._label_for(APPEARANCES, s.appearance, "ダーク"))
        self.output_var.set(s.output_dir)

    def _collect_settings(self) -> None:
        s = self.settings
        s.media_type = self._media_type().value
        s.max_height = VIDEO_QUALITIES[self._video_quality]
        s.audio_bitrate = AUDIO_QUALITIES[self._audio_quality]
        s.prefer_h264 = self.h264_var.get()
        s.embed_thumbnail = self.embed_var.get()
        s.playlist = self.playlist_var.get()
        s.filename_with_url = self.url_name_var.get()
        s.cookies_browser = COOKIE_BROWSERS[self.cookie_menu.get()]
        s.appearance = APPEARANCES[self.appearance_menu.get()]
        s.output_dir = self.output_var.get().strip() or s.output_dir

    def _save_settings(self) -> None:
        self._collect_settings()
        self.settings.save()

    def _report_environment(self) -> None:
        self._append_log("info", f"yt-dlp {yt_dlp.version.__version__} / CustomTkinter {ctk.__version__}")
        ffmpeg = find_ffmpeg()
        if ffmpeg:
            self._append_log("info", f"ffmpeg: {ffmpeg}")
        else:
            self._append_log("warning", "ffmpeg が見つかりません。MP3 変換と高画質 (映像+音声の結合) ができません。README を参照してください。")
        runtimes = detect_js_runtimes()
        if runtimes:
            self._append_log("info", "JavaScript ランタイム: " + ", ".join(runtimes))
        else:
            self._append_log("warning", "deno / node が見つかりません。YouTube の一部の画質が取得できない場合があります (winget install DenoLand.Deno)。")

    # ------------------------------------------------------------------
    # 入力イベント
    # ------------------------------------------------------------------

    def _media_type(self) -> MediaType:
        return MediaType.AUDIO if self.format_switch.get() == FORMAT_LABELS[MediaType.AUDIO] else MediaType.VIDEO

    def _on_media_type_change(self, _label: str) -> None:
        if self._media_type() is MediaType.VIDEO:
            self.quality_menu.configure(values=list(VIDEO_QUALITIES))
            self.quality_menu.set(self._video_quality)
            self.h264_check.configure(state="normal")
        else:
            self.quality_menu.configure(values=list(AUDIO_QUALITIES))
            self.quality_menu.set(self._audio_quality)
            self.h264_check.configure(state="disabled")

    def _on_quality_change(self, label: str) -> None:
        if self._media_type() is MediaType.VIDEO:
            self._video_quality = label
        else:
            self._audio_quality = label

    def _on_appearance_change(self, label: str) -> None:
        ctk.set_appearance_mode(APPEARANCES[label])

    def _paste_from_clipboard(self) -> None:
        try:
            text = self.clipboard_get().strip()
        except tk.TclError:
            text = ""
        if not text:
            self.preview_status.configure(text="クリップボードが空です", text_color=ERROR_TEXT)
            return
        self.url_entry.delete(0, "end")
        self.url_entry.insert(0, text)
        self.url_entry.icursor("end")
        self._check_url_changed()
        self._schedule_preview(delay=0)

    def _clear_url(self) -> None:
        self.url_entry.delete(0, "end")
        self.url_entry.focus_set()
        self._check_url_changed()

    def _browse_output(self) -> None:
        current = self.output_var.get().strip()
        path = filedialog.askdirectory(
            parent=self, title="保存先フォルダーを選択",
            initialdir=current if os.path.isdir(current) else None,
        )
        if path:
            self.output_var.set(os.path.normpath(path))

    def _open_output_dir(self) -> None:
        path = self.output_var.get().strip()
        if not path:
            return
        try:
            os.makedirs(path, exist_ok=True)
            reveal_in_file_manager(path)
        except OSError as exc:
            messagebox.showerror("フォルダーを開けません", str(exc), parent=self)

    # ------------------------------------------------------------------
    # プレビュー
    # ------------------------------------------------------------------

    def _check_url_changed(self) -> None:
        text = self.url_entry.get().strip()
        if text != self._url_text:
            self._url_text = text
            self._schedule_preview()

    def _schedule_preview(self, delay: int = PREVIEW_DELAY_MS) -> None:
        if self._preview_job:
            self.after_cancel(self._preview_job)
            self._preview_job = None
        self._preview_token += 1  # 取得中の古い結果は捨てる
        url = self._url_text
        if not url:
            self._show_preview_placeholder()
        elif not is_valid_url(url):
            self._show_preview_placeholder("http:// または https:// から始まる URL を入力してください", error=True)
        else:
            self._preview_job = self.after(delay, self._start_preview)

    def _start_preview(self) -> None:
        self._preview_job = None
        self._preview_token += 1
        token = self._preview_token
        self.preview_status.configure(text="動画情報を取得中…", text_color=MUTED)
        fetch_info_async(
            self._url_text,
            lambda info, error: self._events.put(("preview", (token, info, error))),
            cookies_browser=COOKIE_BROWSERS[self.cookie_menu.get()] or None,
            playlist=self.playlist_var.get(),
        )

    def _show_preview_placeholder(self, message: str = "", error: bool = False) -> None:
        self._set_thumbnail(None)
        self.title_label.configure(text="URL を入力するとプレビューを表示します")
        self.meta_label.configure(text="")
        self.preview_status.configure(text=message, text_color=ERROR_TEXT if error else MUTED)

    def _on_preview_result(self, token: int, info: Optional[MediaInfo], error: Optional[str]) -> None:
        if token != self._preview_token:
            return
        if error or info is None:
            self._set_thumbnail(None)
            self.title_label.configure(text="動画情報を取得できませんでした")
            self.meta_label.configure(text="")
            summary = (error or "").split("\n\n詳細:")[0]
            self.preview_status.configure(text=summary[:300], text_color=ERROR_TEXT)
            self._append_log("warning", error or "動画情報を取得できませんでした")
            return
        self.title_label.configure(text=info.title)
        parts = []
        if info.uploader:
            parts.append(info.uploader)
        if info.is_playlist:
            parts.append(f"プレイリスト {info.entry_count or '?'} 件")
        elif info.duration:
            parts.append(f"長さ {format_duration(info.duration)}")
        if info.extractor:
            parts.append(info.extractor)
        self.meta_label.configure(text="  •  ".join(parts))
        if info.is_playlist:
            status = f"プレイリストの {info.entry_count or '全'} 件をダウンロードします"
        else:
            status = "ダウンロードできます"
        self.preview_status.configure(text=status, text_color=SUCCESS_TEXT)
        self._set_thumbnail(info.thumbnail_data)

    def _set_thumbnail(self, data: Optional[bytes]) -> None:
        # CTkLabel は image=None への変更が不安定なので毎回作り直す
        if self.thumb_label is not None:
            self.thumb_label.destroy()
        image = None
        if data:
            try:
                pil = Image.open(io.BytesIO(data))
                pil.load()
                pil = pil.convert("RGB")
                scale = min(THUMB_W / pil.width, THUMB_H / pil.height)
                size = (max(1, round(pil.width * scale)), max(1, round(pil.height * scale)))
                pil.thumbnail((size[0] * 2, size[1] * 2), Image.LANCZOS)  # HiDPI 用に 2 倍で保持
                image = ctk.CTkImage(light_image=pil, dark_image=pil, size=size)
            except Exception:
                image = None
        self._thumb_image = image
        if image is not None:
            self.thumb_label = ctk.CTkLabel(self.thumb_frame, text="", image=image)
        else:
            self.thumb_label = ctk.CTkLabel(
                self.thumb_frame, text="No Image", font=self.font_small, text_color=MUTED)
        self.thumb_label.place(relx=0.5, rely=0.5, anchor="center")

    def _on_preview_resize(self, event: tk.Event) -> None:
        scaling = ctk.ScalingTracker.get_widget_scaling(self)
        width = max(200, int(event.width / scaling) - THUMB_W - 56)
        for label in (self.title_label, self.meta_label, self.preview_status):
            label.configure(wraplength=width)

    # ------------------------------------------------------------------
    # ダウンロード
    # ------------------------------------------------------------------

    def _start_download(self) -> None:
        if self._task is not None:
            return
        url = self.url_entry.get().strip()
        if not is_valid_url(url):
            messagebox.showwarning(
                "URL を確認してください", "http:// または https:// から始まる動画の URL を入力してください。", parent=self)
            return
        output_dir = self.output_var.get().strip()
        if not output_dir:
            messagebox.showwarning("保存先を選択してください", "保存先フォルダーを選択してください。", parent=self)
            return

        self._save_settings()
        s = self.settings
        request = DownloadRequest(
            url=url,
            output_dir=output_dir,
            media_type=self._media_type(),
            max_height=s.max_height or None,
            audio_bitrate=s.audio_bitrate,
            prefer_h264=s.prefer_h264,
            embed_thumbnail=s.embed_thumbnail,
            playlist=s.playlist,
            filename_with_url=s.filename_with_url,
            cookies_browser=s.cookies_browser or None,
        )
        self._task = DownloadTask(
            request,
            on_progress=lambda update: self._events.put(("progress", update)),
            on_log=lambda level, message: self._events.put(("log", (level, message))),
            on_finished=lambda result: self._events.put(("finished", result)),
        )
        self._set_busy(True)
        self.progress.set(0)
        self._reset_progress_details()
        self.status_label.configure(text="準備中…", text_color=("gray10", "gray90"))
        self.percent_label.configure(text="")
        kind = "MP3" if request.media_type is MediaType.AUDIO else "MP4"
        self._append_log("info", f"ダウンロード開始 ({kind}): {url}")
        self._task.start()

    def _cancel_download(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        self.cancel_btn.configure(state="disabled", text="キャンセル中…")
        self.status_label.configure(text="キャンセルしています…")

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        for widget in (
            self.url_entry, self.paste_btn, self.clear_btn, self.format_switch, self.quality_menu,
            self.h264_check, self.embed_check, self.playlist_check, self.url_name_check, self.cookie_menu,
            self.output_entry, self.browse_btn, self.download_btn,
        ):
            widget.configure(state=state)
        self.cancel_btn.configure(
            state="normal" if busy else "disabled", text="キャンセル",
            fg_color=DANGER if busy else DISABLED_BUTTON)
        self.download_btn.configure(text="ダウンロード中…" if busy else "ダウンロード開始")
        if not busy:
            self._on_media_type_change(self.format_switch.get())  # MP3 のとき H.264 を無効に戻す

    def _set_indeterminate(self, on: bool) -> None:
        if on == self._indeterminate:
            return
        self._indeterminate = on
        if on:
            self.progress.configure(mode="indeterminate")
            self.progress.start()
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate")

    def _reset_progress_details(self) -> None:
        self.detail_label.configure(text="速度: --    残り時間: --:--    サイズ: -- / --")

    def _apply_progress(self, update: ProgressUpdate) -> None:
        if self._task is None or self._task.cancel_requested:
            return
        prefix = f"[{update.item_index}/{update.item_count}] " if update.item_index and update.item_count else ""
        if update.stage is not Stage.DOWNLOADING:
            self._set_indeterminate(True)
            self.status_label.configure(text=prefix + update.message)
            self.percent_label.configure(text="処理中" if update.stage is Stage.PROCESSING else "")
            return

        stream = {"video": "映像", "audio": "音声"}.get(update.stream or "")
        self.status_label.configure(text=f"{prefix}ダウンロード中" + (f" ({stream})" if stream else ""))
        if update.percent is None:
            self._set_indeterminate(True)
            self.percent_label.configure(text="")
        else:
            self._set_indeterminate(False)
            self.progress.set(update.percent / 100)
            self.percent_label.configure(text=f"{update.percent:.1f}%")
        self.detail_label.configure(text=(
            f"速度: {format_speed(update.speed)}    残り時間: {format_duration(update.eta)}    "
            f"サイズ: {format_bytes(update.downloaded_bytes)} / {format_bytes(update.total_bytes)}"
        ))

    def _on_download_finished(self, result: DownloadResult) -> None:
        self._task = None
        self._set_indeterminate(False)
        self._set_busy(False)

        if result.outcome is Outcome.COMPLETED:
            self.progress.set(1)
            self.percent_label.configure(text="100%")
            self.status_label.configure(text="完了しました", text_color=SUCCESS_TEXT)
            for path in result.files:
                self._append_log("info", f"保存しました: {path}")
            self.bell()
            names = "\n".join(os.path.basename(p) for p in result.files[:5])
            if len(result.files) > 5:
                names += f"\n…ほか {len(result.files) - 5} 件"
            messagebox.showinfo(
                "ダウンロード完了", f"{len(result.files)} 件のファイルを保存しました。\n\n{names}", parent=self)
        elif result.outcome is Outcome.CANCELLED:
            self.progress.set(0)
            self.percent_label.configure(text="")
            self._reset_progress_details()
            self.status_label.configure(text="キャンセルしました", text_color=MUTED)
            self._append_log("info", "ダウンロードをキャンセルしました")
        else:
            self.percent_label.configure(text="")
            self.status_label.configure(text="失敗しました", text_color=ERROR_TEXT)
            self._append_log("error", result.error or "不明なエラー")
            messagebox.showerror("ダウンロード失敗", result.error or "不明なエラーが発生しました。", parent=self)

    # ------------------------------------------------------------------
    # ワーカースレッド → メインスレッド
    # ------------------------------------------------------------------

    def _poll_events(self) -> None:
        try:
            self._check_url_changed()
            pending: Optional[ProgressUpdate] = None
            for _ in range(1000):
                try:
                    kind, payload = self._events.get_nowait()
                except queue.Empty:
                    break
                if kind == "progress":
                    # 同じ段階の進捗は最新の 1 件だけ反映すれば十分。段階が変わるときは
                    # 「ダウンロード完了」などを取りこぼさないよう前の分を先に反映する
                    if pending is not None and pending.stage is not payload.stage:
                        self._apply_progress(pending)
                    pending = payload
                    continue
                if pending is not None:
                    self._apply_progress(pending)
                    pending = None
                if kind == "log":
                    self._append_log(*payload)
                elif kind == "preview":
                    self._on_preview_result(*payload)
                elif kind == "finished":
                    self._on_download_finished(payload)
            if pending is not None:
                self._apply_progress(pending)
        finally:
            self.after(POLL_MS, self._poll_events)

    def _append_log(self, level: str, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"{stamp}  {message}\n", level if level in ("warning", "error") else None)
        lines = int(self.log_box.index("end-1c").split(".")[0])
        if lines > LOG_MAX_LINES:
            self.log_box.delete("1.0", f"{lines - LOG_MAX_LINES}.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def report_callback_exception(self, exc, val, tb) -> None:  # noqa: D401 - Tk の API
        """Tk のコールバックで起きた例外を握りつぶさずログとダイアログに出す."""
        detail = "".join(traceback.format_exception(exc, val, tb))
        try:
            self._append_log("error", detail)
        except tk.TclError:
            pass
        messagebox.showerror("予期しないエラー", f"{val}\n\n詳細はログを確認してください。", parent=self)

    def _on_close(self) -> None:
        if self._task is not None and self._task.is_running:
            if not messagebox.askyesno("終了確認", "ダウンロード中です。中止して終了しますか？", parent=self):
                return
            self._task.cancel()
            self._task.join(timeout=5)
        self._save_settings()
        self.destroy()


def run() -> None:
    app = DropItoApp()
    app.mainloop()
