# Build with: /usr/bin/python3 -m PyInstaller jukebox_macos.spec
#
# Produces dist/Notorious BPM.app -- a standalone bundle with its own
# Python interpreter, instead of the previous hand-built .app whose launcher
# script execed the *system* Command Line Tools Python (see
# Contents/MacOS/JukeboxPlayer in dist-macos/ for that old approach, from
# back when this was still called Jukebox).
#
# Why this exists: macOS's file-access permission (Privacy & Security >
# Files and Folders / Full Disk Access) for reading an external drive is
# granted per-binary, keyed off that binary's code signature. The CLT
# Python.app is Apple's shared system interpreter, used by every CLI tool on
# the machine -- a routine `xcode-select`/Command Line Tools update changes
# its signature, and macOS silently drops every permission grant tied to
# it, including this app's. Bundling our own interpreter here means the
# grant is keyed to *this app's own build* instead -- it only needs
# re-granting when this app itself is rebuilt, not on unrelated system
# updates.
import os
from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

datas = [("static", "static")]
datas += collect_data_files("langdetect")

icon_path = "assets/AppIcon.icns"
icon_path = icon_path if os.path.isfile(icon_path) else None

a = Analysis(
    ["desktop_macos.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    # app.py's various maintenance routes (tag fixing, genre/year fill,
    # duplicate cleanup, format conversion, the folder picker) import these
    # lazily, function-local, so PyInstaller's static analysis can miss
    # them -- listed explicitly the same way the Windows spec already lists
    # its own platform-specific lazy imports.
    hiddenimports=[
        "mutagen", "mutagen.flac", "mutagen.mp3", "mutagen.mp4",
        "mutagen.easyid3", "mutagen.easymp4", "mutagen.id3",
        "werkzeug.serving",
        "webview.platforms.cocoa",
        "scan_library", "fill_genres", "fill_years", "unify_artist_genre",
        "fix_artist_title", "convert_audio",
        "tkinter", "tkinter.filedialog",
        "objc", "Cocoa", "WebKit", "Quartz",
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
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="NotoriousBPM",
)

app = BUNDLE(
    coll,
    name="Notorious BPM.app",
    icon=icon_path,
    bundle_identifier="local.ivan.notorious-bpm",
    info_plist={
        "CFBundleDisplayName": "Notorious B.P.M.",
        "CFBundleName": "Notorious B.P.M.",
        "CFBundleShortVersionString": "1.2",
        "CFBundleVersion": "1.2",
        "LSApplicationCategoryType": "public.app-category.music",
        "LSMinimumSystemVersion": "11.0",
        "LSUIElement": False,
        "NSHighResolutionCapable": True,
        # Without these two keys, macOS silently blocks this app's Bonjour
        # discovery -- app.js's AirPlay button (webkitShowPlaybackTargetPicker,
        # see static/app.js) depends on WKWebView finding _airplay._tcp/
        # _raop._tcp devices on the LAN, which never surfaces without a
        # granted Local Network permission, and the OS never even shows that
        # permission prompt without NSLocalNetworkUsageDescription present.
        # The button just stays permanently hidden in the packaged .app with
        # no error -- it works fine in a plain browser tab, which already
        # carries this permission system-wide, which is what made this so
        # easy to miss.
        "NSLocalNetworkUsageDescription": "Notorious B.P.M. uses your local network to find AirPlay speakers and Apple TVs to stream music to.",
        "NSBonjourServices": ["_airplay._tcp", "_raop._tcp"],
        # Libraries and music routinely live in protected places (Desktop,
        # Documents, external/network volumes). Without a purpose string for
        # each, macOS has nothing to show in its permission prompt and the
        # access can end up silently denied instead of asked about.
        "NSDesktopFolderUsageDescription": "Notorious B.P.M. reads your music library and music files when they are stored on your Desktop.",
        "NSDocumentsFolderUsageDescription": "Notorious B.P.M. reads your music library and music files when they are stored in Documents.",
        "NSDownloadsFolderUsageDescription": "Notorious B.P.M. reads music files you keep in Downloads.",
        "NSRemovableVolumesUsageDescription": "Notorious B.P.M. reads your music library and music files stored on external drives.",
        "NSNetworkVolumesUsageDescription": "Notorious B.P.M. reads your music library and music files stored on network drives.",
    },
)
