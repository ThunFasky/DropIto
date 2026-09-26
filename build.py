"""DropIto を PyInstaller で実行ファイル化するビルドスクリプト.

Windows で実行すると dist/DropIto.exe ができる::

    python build.py                   # dist/DropIto.exe (1 ファイル)
    python build.py --download-tools  # ffmpeg / deno を bin/ に取得してからビルド
    python build.py --onedir          # フォルダー形式 (起動が速い)
    python build.py --embed-tools     # ffmpeg / deno も exe に内蔵 (配布は楽、起動は遅い)
    python build.py --check           # ビルドせずに設定と PyInstaller 引数を検証
    python build.py --dry-run         # 実行する PyInstaller コマンドを表示するだけ

bin/ に置いた ffmpeg / ffprobe / deno は、既定では exe と同じ場所の dist/bin/ に
コピーされる (downloader.find_executable がそこを探す)。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform
import py_compile
import re
import shlex
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

APP_NAME = "DropIto"
APP_VERSION = "1.0.0"
ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / "assets"
TOOLS_DIR = ROOT / "bin"
BUILD_DIR = ROOT / "build"
DIST_DIR = ROOT / "dist"
ICON_ICO = ASSETS / "icon.ico"
ICON_PNG = ASSETS / "icon.png"
SOURCES = ("main.py", "ui.py", "downloader.py", "settings.py")

IS_WINDOWS = sys.platform == "win32"
EXE = ".exe" if IS_WINDOWS else ""
TOOL_NAMES = ("ffmpeg", "ffprobe", "deno")

FFMPEG_RELEASE = "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest"
DENO_RELEASE = "https://github.com/denoland/deno/releases/latest/download"


# ---------------------------------------------------------------------------
# アイコン
# ---------------------------------------------------------------------------


def draw_icon(size: int):
    """下向き矢印 + トレイのアイコンを描く (外部画像に頼らない)."""
    from PIL import Image, ImageDraw

    scale = 4  # アンチエイリアスのため大きく描いて縮小
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.22), fill=(31, 106, 165, 255))
    white = (255, 255, 255, 255)
    cx = s / 2
    shaft_w = s * 0.14
    d.rectangle((cx - shaft_w / 2, s * 0.18, cx + shaft_w / 2, s * 0.50), fill=white)
    d.polygon([(cx - s * 0.24, s * 0.44), (cx + s * 0.24, s * 0.44), (cx, s * 0.70)], fill=white)
    line = s * 0.075
    d.rounded_rectangle((s * 0.20, s * 0.74, s * 0.80, s * 0.74 + line), radius=int(line / 2), fill=white)
    return img.resize((size, size), Image.LANCZOS)


def ensure_icons(force: bool = False) -> None:
    if ICON_ICO.exists() and ICON_PNG.exists() and not force:
        return
    ASSETS.mkdir(exist_ok=True)
    big = draw_icon(256)
    big.save(ICON_ICO, format="ICO", sizes=[(n, n) for n in (16, 24, 32, 48, 64, 128, 256)])
    draw_icon(128).save(ICON_PNG, format="PNG")
    print(f"アイコンを生成しました: {ICON_ICO.relative_to(ROOT)}, {ICON_PNG.relative_to(ROOT)}")


# ---------------------------------------------------------------------------
# ffmpeg / deno の取得
# ---------------------------------------------------------------------------


def _download(url: str) -> bytes:
    print(f"  取得中: {url}")
    request = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}-build"})
    with urllib.request.urlopen(request, timeout=60) as response:
        total = int(response.headers.get("Content-Length") or 0)
        buf = io.BytesIO()
        while chunk := response.read(1 << 20):
            buf.write(chunk)
            if total:
                print(f"\r  {buf.tell() / total * 100:5.1f}% ({buf.tell() >> 20} / {total >> 20} MB)", end="", flush=True)
        print()
    return buf.getvalue()


def _verify_sha256(data: bytes, checksum_text: str, filename: str) -> None:
    # "hash  filename" 形式 (複数行) と、ハッシュが 1 つだけのファイルの両方に対応
    hash_re = re.compile(r"\b([0-9a-fA-F]{64})\b")
    expected = next(
        (m.group(1).lower() for line in checksum_text.splitlines()
         if filename in line and (m := hash_re.search(line))),
        None,
    )
    if expected is None:
        hashes = {h.lower() for h in hash_re.findall(checksum_text)}
        expected = hashes.pop() if len(hashes) == 1 else None
    if not expected:
        raise RuntimeError(f"{filename} のチェックサムが見つかりません")
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise RuntimeError(f"{filename} のチェックサムが一致しません (期待 {expected}, 実際 {actual})")
    print(f"  SHA256 OK: {filename}")


def _tool_assets() -> tuple:
    machine = platform.machine().lower()
    if IS_WINDOWS:
        arch = "winarm64" if machine in ("arm64", "aarch64") else "win64"
        ffmpeg = f"ffmpeg-master-latest-{arch}-gpl.zip"
        deno = "deno-aarch64-pc-windows-msvc.zip" if arch == "winarm64" else "deno-x86_64-pc-windows-msvc.zip"
    elif sys.platform.startswith("linux"):
        arm = machine in ("arm64", "aarch64")
        ffmpeg = f"ffmpeg-master-latest-{'linuxarm64' if arm else 'linux64'}-gpl.tar.xz"
        deno = f"deno-{'aarch64' if arm else 'x86_64'}-unknown-linux-gnu.zip"
    else:
        raise RuntimeError("--download-tools は Windows / Linux のみ対応です (macOS は brew install ffmpeg deno)")
    return ffmpeg, deno


def _extract_members(archive: bytes, filename: str, wanted: dict) -> None:
    """アーカイブから wanted {アーカイブ内のファイル名: 保存先} を取り出す."""
    found = set()
    if filename.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(archive)) as zf:
            for info in zf.infolist():
                base = os.path.basename(info.filename)
                if base in wanted:
                    wanted[base].write_bytes(zf.read(info))
                    found.add(base)
    else:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as tf:
            for member in tf.getmembers():
                base = os.path.basename(member.name)
                if base in wanted and member.isfile():
                    wanted[base].write_bytes(tf.extractfile(member).read())
                    found.add(base)
    missing = set(wanted) - found
    if missing:
        raise RuntimeError(f"{filename} に {', '.join(sorted(missing))} が含まれていません")
    for path in wanted.values():
        path.chmod(0o755)
        print(f"  配置しました: {path.relative_to(ROOT)}")


def download_tools(force: bool = False) -> None:
    """ffmpeg (yt-dlp 向けパッチ済みビルド) と deno を bin/ に取得する."""
    TOOLS_DIR.mkdir(exist_ok=True)
    ffmpeg_asset, deno_asset = _tool_assets()

    ffmpeg_targets = {f"{n}{EXE}": TOOLS_DIR / f"{n}{EXE}" for n in ("ffmpeg", "ffprobe")}
    if force or not all(p.exists() for p in ffmpeg_targets.values()):
        print("ffmpeg を取得します")
        checksums = _download(f"{FFMPEG_RELEASE}/checksums.sha256").decode()
        data = _download(f"{FFMPEG_RELEASE}/{ffmpeg_asset}")
        _verify_sha256(data, checksums, ffmpeg_asset)
        _extract_members(data, ffmpeg_asset, ffmpeg_targets)
    else:
        print("ffmpeg は取得済みです (再取得は --force-tools)")

    deno_target = TOOLS_DIR / f"deno{EXE}"
    if force or not deno_target.exists():
        print("deno を取得します")
        checksum = _download(f"{DENO_RELEASE}/{deno_asset}.sha256sum").decode("utf-8", "replace")
        data = _download(f"{DENO_RELEASE}/{deno_asset}")
        _verify_sha256(data, checksum, deno_asset)
        _extract_members(data, deno_asset, {f"deno{EXE}": deno_target})
    else:
        print("deno は取得済みです (再取得は --force-tools)")


def available_tools() -> list:
    return [TOOLS_DIR / f"{n}{EXE}" for n in TOOL_NAMES if (TOOLS_DIR / f"{n}{EXE}").exists()]


# ---------------------------------------------------------------------------
# PyInstaller
# ---------------------------------------------------------------------------


def write_version_file() -> Path:
    """exe のプロパティ (詳細タブ) に出るバージョン情報."""
    major, minor, patch = (int(x) for x in APP_VERSION.split("."))
    text = f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(filevers=({major}, {minor}, {patch}, 0), prodvers=({major}, {minor}, {patch}, 0),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041104B0', [
      StringStruct('CompanyName', 'ThunFasky'),
      StringStruct('FileDescription', '{APP_NAME} - 動画・音声ダウンローダー'),
      StringStruct('FileVersion', '{APP_VERSION}'),
      StringStruct('InternalName', '{APP_NAME}'),
      StringStruct('OriginalFilename', '{APP_NAME}.exe'),
      StringStruct('ProductName', '{APP_NAME}'),
      StringStruct('ProductVersion', '{APP_VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [0x0411, 1200])])
  ]
)
"""
    BUILD_DIR.mkdir(exist_ok=True)
    path = BUILD_DIR / "version_info.txt"
    path.write_text(text, encoding="utf-8")
    return path


def pyinstaller_args(opts: argparse.Namespace) -> list:
    sep = os.pathsep  # --add-data の区切り (Windows は ';')
    args = [
        str(ROOT / "main.py"),
        "--name", APP_NAME,
        "--noconfirm",
        "--onedir" if opts.onedir else "--onefile",
        "--console" if opts.console else "--windowed",
        "--distpath", str(opts.distpath),
        "--workpath", str(BUILD_DIR),
        "--specpath", str(BUILD_DIR),
        "--icon", str(ICON_ICO),
        "--add-data", f"{ASSETS}{sep}assets",
        # CustomTkinter のテーマ JSON / フォント
        "--collect-data", "customtkinter",
        # CTkImage (PIL.ImageTk) が動的 import する。無いと exe でサムネイル表示が落ちる
        "--hidden-import", "PIL._tkinter_finder",
        # YouTube の JS チャレンジ解読スクリプト (yt-dlp が動的 import する)
        "--hidden-import", "yt_dlp_ejs",
        "--collect-data", "yt_dlp_ejs",
        # 使わない重いモジュールを除外してサイズを減らす
        "--exclude-module", "pytest",
        "--exclude-module", "numpy",
    ]
    if IS_WINDOWS:
        args += ["--version-file", str(BUILD_DIR / "version_info.txt")]
    if opts.clean:
        args.append("--clean")
    if opts.embed_tools:
        for tool in available_tools():
            args += ["--add-binary", f"{tool}{sep}bin"]
    return args


def check_configuration(args: list) -> bool:
    """ビルドせずに、ソース・同梱ファイル・PyInstaller 引数を検証する."""
    ok = True

    def report(passed: bool, message: str) -> None:
        nonlocal ok
        ok &= passed
        print(f"  [{'OK' if passed else 'NG'}] {message}")

    print("構成チェック:")
    for name in SOURCES:
        path = ROOT / name
        try:
            py_compile.compile(str(path), cfile=os.path.join(tempfile.gettempdir(), f"dropito_{name}c"), doraise=True)
            report(True, f"{name} の構文")
        except (py_compile.PyCompileError, FileNotFoundError) as exc:
            report(False, f"{name} の構文: {exc}")

    for module in ("PyInstaller", "customtkinter", "yt_dlp", "yt_dlp_ejs", "PIL"):
        try:
            mod = __import__(module)
            report(True, f"{module} {getattr(mod, '__version__', '')}".rstrip())
        except ImportError as exc:
            report(False, f"{module} を import できません: {exc}")

    try:
        from PyInstaller.__main__ import generate_parser

        parsed = generate_parser().parse_args(args)
        report(True, f"PyInstaller 引数の解析 (onefile={parsed.onefile}, console={parsed.console}, name={parsed.name})")
        for value in parsed.datas + parsed.binaries:
            # PyInstaller 6 は (src, dest) のタプル、古い版は "src<sep>dest" 文字列
            src = value[0] if isinstance(value, tuple) else value.rsplit(os.pathsep, 1)[0]
            report(os.path.exists(src), f"同梱ファイル {src}")
        report(os.path.exists(parsed.icon_file[0]) if parsed.icon_file else False, "アイコン")
    except SystemExit:
        report(False, "PyInstaller が引数を受け付けませんでした (上のエラーを参照)")
    except ImportError:
        pass  # PyInstaller 未インストールは上で NG 済み

    tools = [p.name for p in available_tools()]
    print(f"  [--] bin/ の外部ツール: {', '.join(tools) if tools else 'なし (python build.py --download-tools で取得可)'}")
    return ok


def copy_tools_next_to_exe(distpath: Path, onedir: bool) -> None:
    tools = available_tools()
    if not tools:
        print("注意: bin/ に ffmpeg が無いため同梱していません。利用者の PC に ffmpeg が必要です。")
        return
    target = (distpath / APP_NAME if onedir else distpath) / "bin"
    target.mkdir(parents=True, exist_ok=True)
    for tool in tools:
        shutil.copy2(tool, target / tool.name)
    print(f"外部ツールをコピーしました: {target} ({', '.join(t.name for t in tools)})")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} の実行ファイルを PyInstaller でビルドする")
    parser.add_argument("--onedir", action="store_true", help="1 ファイルではなくフォルダー形式でビルド")
    parser.add_argument("--console", action="store_true", help="デバッグ用にコンソールを表示する")
    parser.add_argument("--clean", action="store_true", help="PyInstaller のキャッシュを消してからビルド")
    parser.add_argument("--download-tools", action="store_true", help="ffmpeg / deno を bin/ に取得する")
    parser.add_argument("--force-tools", action="store_true", help="取得済みでも ffmpeg / deno を取り直す")
    parser.add_argument("--embed-tools", action="store_true", help="bin/ のツールを exe に内蔵する")
    parser.add_argument("--distpath", type=Path, default=DIST_DIR, help="出力先 (既定: dist/)")
    parser.add_argument("--check", action="store_true", help="ビルドせずに構成を検証する")
    parser.add_argument("--dry-run", action="store_true", help="PyInstaller のコマンドを表示するだけ")
    parser.add_argument("--regen-icon", action="store_true", help="アイコンを作り直す")
    opts = parser.parse_args(argv)
    opts.distpath = opts.distpath.resolve()

    ensure_icons(force=opts.regen_icon)
    if IS_WINDOWS or opts.check or opts.dry_run:
        write_version_file()
    if opts.download_tools or opts.force_tools:
        download_tools(force=opts.force_tools)

    args = pyinstaller_args(opts)
    command = shlex.join([sys.executable, "-m", "PyInstaller", *args])
    if opts.dry_run or opts.check:
        print("PyInstaller コマンド:")
        print(f"  {command}\n")
    if opts.check:
        return 0 if check_configuration(args) else 1
    if opts.dry_run:
        return 0

    if not IS_WINDOWS:
        print("注意: Windows 以外では、その OS 用の実行ファイルができます (.exe は Windows でビルドしてください)")
    import PyInstaller.__main__

    PyInstaller.__main__.run(args)
    if not opts.embed_tools:
        copy_tools_next_to_exe(opts.distpath, opts.onedir)
    exe = opts.distpath / (APP_NAME if not opts.onedir else f"{APP_NAME}/{APP_NAME}")
    print(f"\nビルド完了: {exe}{EXE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
