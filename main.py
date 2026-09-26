"""DropIto エントリーポイント.

    python main.py
"""

from __future__ import annotations

import os
import sys
import traceback


def _fix_std_streams() -> None:
    """PyInstaller の --windowed ビルドでは sys.stdout / stderr が None になる.

    yt-dlp などが書き込もうとして落ちるので、捨て先を用意しておく。
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


def _set_app_user_model_id() -> None:
    """タスクバーで python.exe ではなく DropIto のアイコンとしてまとめる (Windows)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ThunFasky.DropIto")
    except Exception:
        pass


def _report_fatal_error() -> None:
    """起動に失敗したとき、コンソールが無くても原因が分かるようにする."""
    detail = traceback.format_exc()
    try:
        from settings import config_dir

        log_dir = config_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "crash.log").write_text(detail, encoding="utf-8")
    except Exception:
        pass
    try:
        from tkinter import Tk, messagebox

        root = Tk()
        root.withdraw()
        messagebox.showerror("DropIto を起動できません", detail[-2000:])
        root.destroy()
    except Exception:
        print(detail, file=sys.stderr)


def main() -> int:
    _fix_std_streams()
    _set_app_user_model_id()
    try:
        from ui import run

        run()
    except Exception:
        _report_fatal_error()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
