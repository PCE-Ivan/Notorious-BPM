# Build with: pyinstaller jukebox.spec
# Produces dist/NotoriousBPM/NotoriousBPM.exe (a "onedir" build -- an .exe
# plus its supporting files in one folder, which Inno Setup then packages
# up).
import os
from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

# langdetect ships its per-language n-gram profiles as package data, loaded
# by path at runtime -- PyInstaller's import analysis won't pick those up on
# its own since they're not imported as Python modules. Likewise pywebview's
# Windows (EdgeChromium) backend ships some supporting .NET assemblies as
# package data rather than importable Python -- collect_data_files grabs
# those the same way. (Untested here -- no Windows machine available. If
# the built exe can't find its WebView2 loader assemblies at runtime, this
# is the first place to check; see BUILD_WINDOWS.md.)
datas = [("static", "static")]
datas += collect_data_files("langdetect")
datas += collect_data_files("webview")
if os.path.isfile("runtimeconfig.json"):
    datas.append(("runtimeconfig.json", "."))

icon_path = "icon.ico" if os.path.isfile("icon.ico") else None

a = Analysis(
    ["launcher.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "mutagen.flac", "mutagen.mp3", "mutagen.mp4",
        "mutagen.easyid3", "mutagen.easymp4", "mutagen.id3",
        "werkzeug.serving",
        "webview.platforms.edgechromium",
        "webview.platforms.winforms",
        "webview.platforms.mshtml",
        "clr_loader",
        "clr_loader.ffi",
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
    icon=icon_path,
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
