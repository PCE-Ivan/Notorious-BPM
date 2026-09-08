# Build with: pyinstaller jukebox_linux.spec
# Produces dist/NotoriousBPM/NotoriousBPM (a "onedir" build -- a Linux ELF
# executable plus its supporting files in one folder). See BUILD_LINUX.md
# for the system packages this needs first and how to package the result.
import os
from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

# langdetect ships its per-language n-gram profiles as package data, loaded
# by path at runtime -- PyInstaller's import analysis won't pick those up on
# its own since they're not imported as Python modules.
datas = [("static", "static")]
datas += collect_data_files("langdetect")

a = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "mutagen.flac", "mutagen.mp3", "mutagen.mp4",
        "mutagen.easyid3", "mutagen.easymp4", "mutagen.id3",
        "werkzeug.serving",
        # pywebview's Linux backend -- GTK (WebKit2GTK) is the lightweight,
        # native option and what BUILD_LINUX.md installs; if that backend
        # can't initialize at runtime, pywebview falls back to Qt if
        # PyQt5/PySide2 happens to be installed, which is why both are
        # listed here rather than just gtk.
        "webview.platforms.gtk",
        "webview.platforms.qt",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NotoriousBPM",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,  # no terminal window -- a real double-click desktop app
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="NotoriousBPM",
)
