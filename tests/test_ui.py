"""ui.py のスモークテスト (ネットワーク不要・ディスプレイが必要).

Linux のヘッドレス環境では xvfb-run -a pytest tests/test_ui.py で実行する。
"""

from __future__ import annotations

import tkinter as tk

import pytest

pytestmark = pytest.mark.gui


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))
    try:
        tk.Tk().destroy()
    except tk.TclError as exc:
        pytest.skip(f"ディスプレイがありません: {exc}")

    import ui

    popups = []
    for name in ("askyesno", "showerror", "showwarning", "showinfo"):
        monkeypatch.setattr(ui.messagebox, name, lambda *a, _n=name, **k: popups.append((_n, a)) or False)
    window = ui.DropItoApp()
    window.popups = popups
    window.update()
    yield window
    window.destroy()


def pump(window, ms=100):
    window.after(ms, window.quit)
    window.mainloop()


def test_format_switch_changes_quality_choices(app):
    import ui

    assert app.quality_menu.cget("values") == list(ui.VIDEO_QUALITIES)
    app.quality_menu.set("720p (HD)")
    app._on_quality_change("720p (HD)")

    app.format_switch.set(ui.FORMAT_LABELS[ui.MediaType.AUDIO])
    app._on_media_type_change(app.format_switch.get())
    assert app.quality_menu.cget("values") == list(ui.AUDIO_QUALITIES)
    assert app.quality_menu.get() == "320kbps (高音質)"
    assert app.h264_check.cget("state") == "disabled"

    app.format_switch.set(ui.FORMAT_LABELS[ui.MediaType.VIDEO])
    app._on_media_type_change(app.format_switch.get())
    assert app.quality_menu.get() == "720p (HD)"  # 動画側の選択は覚えている
    assert app.h264_check.cget("state") == "normal"


def test_invalid_url_shows_hint(app):
    app.url_entry.insert(0, "youtube.com/watch?v=abc")
    pump(app)
    assert "https://" in app.preview_status.cget("text")


def test_start_with_invalid_url_warns(app):
    app.url_entry.insert(0, "not a url")
    app._start_download()
    assert app._task is None
    assert app.popups and app.popups[-1][0] == "showwarning"


def test_busy_state_and_progress(app):
    import ui
    from downloader import ProgressUpdate, Stage

    app._task = type("FakeTask", (), {"cancel_requested": False})()
    app._set_busy(True)
    assert app.download_btn.cget("state") == "disabled"
    assert app.cancel_btn.cget("state") == "normal"

    app._apply_progress(ProgressUpdate(Stage.DOWNLOADING, percent=42.0, downloaded_bytes=42 * 1024 ** 2,
                                       total_bytes=100 * 1024 ** 2, speed=2 * 1024 ** 2, eta=29, stream="video"))
    assert app.percent_label.cget("text") == "42.0%"
    assert app.status_label.cget("text") == "ダウンロード中 (映像)"
    assert "2.0 MB/s" in app.detail_label.cget("text") and "0:29" in app.detail_label.cget("text")
    assert app.progress.get() == pytest.approx(0.42)

    app._apply_progress(ProgressUpdate(Stage.PROCESSING, message="MP3 に変換中…"))
    assert app.status_label.cget("text") == "MP3 に変換中…"
    assert app.progress.cget("mode") == "indeterminate"

    app._on_download_finished(ui.DownloadResult(ui.Outcome.FAILED, error="テスト用のエラー"))
    assert app._task is None
    assert app.status_label.cget("text") == "失敗しました"
    assert app.download_btn.cget("state") == "normal"
    assert app.cancel_btn.cget("state") == "disabled"
    assert app.popups[-1] == ("showerror", ("ダウンロード失敗", "テスト用のエラー"))


def test_settings_roundtrip(app, tmp_path):
    import ui
    from settings import Settings

    app.format_switch.set(ui.FORMAT_LABELS[ui.MediaType.AUDIO])
    app._on_media_type_change(app.format_switch.get())
    app.quality_menu.set("192kbps (標準)")
    app._on_quality_change("192kbps (標準)")
    app.cookie_menu.set("Firefox")
    app.output_var.set(str(tmp_path))
    assert app.url_name_var.get() is True  # 既定はオン
    assert app.all_videos_var.get() is True
    app.url_name_var.set(False)
    app.all_videos_var.set(False)
    app._save_settings()

    loaded = Settings.load()
    assert (loaded.media_type, loaded.audio_bitrate, loaded.cookies_browser, loaded.output_dir,
            loaded.filename_with_url, loaded.all_post_videos) == ("mp3", 192, "firefox", str(tmp_path), False, False)


def test_stage_change_is_not_coalesced_away(app):
    from downloader import ProgressUpdate, Stage

    app._task = type("FakeTask", (), {"cancel_requested": False})()
    # 1 回のポーリングで「ダウンロード中 → 完了 → 後処理」がまとめて届くケース
    for update in (
        ProgressUpdate(Stage.DOWNLOADING, percent=1.0, downloaded_bytes=1024, total_bytes=500 * 1024, speed=5000),
        ProgressUpdate(Stage.DOWNLOADING, percent=100.0, downloaded_bytes=500 * 1024, total_bytes=500 * 1024),
        ProgressUpdate(Stage.PROCESSING, message="ファイルを修正中…"),
    ):
        app._events.put(("progress", update))
    pump(app)
    assert "500.0 KB / 500.0 KB" in app.detail_label.cget("text")
    assert app.status_label.cget("text") == "ファイルを修正中…"
    app._task = None


def test_completion_does_not_ask_to_open_folder(app, tmp_path):
    import ui

    saved = tmp_path / "video.mp4"
    saved.write_bytes(b"")
    app._task = type("FakeTask", (), {"cancel_requested": False})()
    app._on_download_finished(ui.DownloadResult(ui.Outcome.COMPLETED, files=[str(saved)]))
    assert app.status_label.cget("text") == "完了しました"
    kinds = [kind for kind, _ in app.popups]
    assert kinds == ["showinfo"]  # 「フォルダーを開きますか？」の確認は出さない
    title, message = app.popups[0][1]
    assert title == "ダウンロード完了" and "video.mp4" in message and "開きますか" not in message
