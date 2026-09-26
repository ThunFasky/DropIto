"""ユーザー設定の保存/読み込み (%APPDATA%\\DropIto\\settings.json)."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

APP_NAME = "DropIto"


def config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME


def default_download_dir() -> str:
    """Windows の「ダウンロード」フォルダ (場所を移動していても追従する)."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            from uuid import UUID

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                            ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

            folder_id = GUID.from_buffer_copy(UUID("{374DE290-123F-4565-9164-39C4925E467B}").bytes_le)
            path_ptr = ctypes.c_wchar_p()
            shell32 = ctypes.windll.shell32
            if shell32.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(path_ptr)) == 0:
                path = path_ptr.value
                ctypes.windll.ole32.CoTaskMemFree(path_ptr)
                if path:
                    return path
        except Exception:
            pass
    downloads = Path.home() / "Downloads"
    return str(downloads if downloads.is_dir() else Path.home())


@dataclass
class Settings:
    output_dir: str = ""
    media_type: str = "mp4"  # "mp4" / "mp3"
    max_height: int = 0  # 0 = 最高画質
    audio_bitrate: int = 320
    prefer_h264: bool = False
    embed_thumbnail: bool = True
    playlist: bool = False
    cookies_browser: str = ""  # "" = 使わない
    filename_with_url: bool = True  # ファイル名を「タイトル -ダウンロード元URL」にする
    appearance: str = "dark"  # "dark" / "light" / "system"

    @classmethod
    def path(cls) -> Path:
        return config_dir() / "settings.json"

    @classmethod
    def load(cls) -> "Settings":
        settings = cls()
        try:
            data = json.loads(cls.path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        for f in fields(cls):
            value = data.get(f.name)
            if value is not None and isinstance(value, type(getattr(settings, f.name))):
                setattr(settings, f.name, value)
        if not settings.output_dir or not os.path.isdir(settings.output_dir):
            settings.output_dir = default_download_dir()
        return settings

    def save(self) -> None:
        try:
            path = self.path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass  # 設定が保存できなくてもアプリは動かす
