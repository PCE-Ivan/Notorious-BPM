"""One command: demo library -> frames -> narration -> video.

    python3 make_video.py            full quality (1080p)
    python3 make_video.py --draft    960x540, fast: checks timing and wiring

Needs: Google Chrome, ffmpeg + ffprobe, the macOS `say` voice "Daniel (Enhanced)" (System Settings > Accessibility >
Spoken Content > Manage Voices), `pip install websockets`, and fpcalc (chromaprint) for the duplicates scene.
Takes ~15 minutes, most of it the real fingerprinting run in the duplicates scene. Everything lands in the work
folder (PITCH_WORK, default ./work); see README.md."""
import json
import os
import subprocess
import sys
import time
import urllib.request

import cards
import lib
import run
import scenes
import setup_library
import thumb
from paths import CODE, WORK

PORT = 5788


def stop_server():
    out = subprocess.run(["lsof", f"-ti:{PORT}"], capture_output=True, text=True).stdout.split()
    for pid in out:
        subprocess.run(["kill", pid])
    time.sleep(1.5)


def start_server():
    stop_server()
    # A sandboxed HOME so the app's own log/config defaults can never land in the real one.
    env = dict(os.environ, HOME=os.path.join(WORK, "home"))
    os.makedirs(env["HOME"], exist_ok=True)
    env["PYTHONUSERBASE"] = subprocess.run([sys.executable, "-c", "import site; print(site.USER_BASE)"],
                                           capture_output=True, text=True, env=os.environ).stdout.strip()
    subprocess.Popen([sys.executable, os.path.join(CODE, "serve.py")], env=env, cwd=CODE,
                     stdout=open(os.path.join(WORK, "serve.log"), "w"), stderr=subprocess.STDOUT)
    for _ in range(40):
        time.sleep(0.5)
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/instance", timeout=2).read()
            return
        except Exception:
            pass
    raise RuntimeError("scratch server didn't start; see work/serve.log")


def wait_idle():
    time.sleep(1.5)
    while lib.api("/api/scan-progress")["running"]:
        time.sleep(0.5)
    time.sleep(1.5)
    while lib.api("/api/art/warm/progress")["running"]:
        time.sleep(0.5)


def main():
    draft = "--draft" in sys.argv
    skip_scenes = "--skip-scenes" in sys.argv   # resume: frames already shot, redo cards/narration/video only
    setup_library.build()
    start_server()
    if not skip_scenes:
        lib.api("/api/rescan", "POST", {})
        wait_idle()
        print("library:", lib.api("/api/facets")["total"], "tracks")

        # Phase A: the library as it looks before the duplicates are added back
        run.run_scenes(["library", "search", "keys", "skins", "airplay", "looks", "radio", "ipod", "thumb"])

        # Phase B: duplicates present, fingerprints forgotten, and a server with no memory of an earlier run
        scenes.prep_phase_b()
        start_server()
        run.run_scenes(["dups", "tags", "undo", "import"])

    # The cards and thumbnail load cover art from the running app, so the server stays up until here.
    import asyncio
    asyncio.run(cards.main())
    asyncio.run(thumb.main())
    stop_server()
    subprocess.run([sys.executable, os.path.join(CODE, "narration.py")], check=True, cwd=CODE)
    subprocess.run([sys.executable, os.path.join(CODE, "assemble.py")] + (["--draft"] if draft else []), check=True, cwd=CODE)
    print("done ->", os.path.join(WORK, "out"))


if __name__ == "__main__":
    main()
