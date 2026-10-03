"""Builds the demo library's files under the work folder (never touches the source music):

  music/        a tree of SYMLINKS to the source tracks (so nothing is ever copied or written to them),
                minus INBOX_TRACKS
  inbox/        symlinks to INBOX_TRACKS -- what the drag-and-drop scene "drops"
  dupes_staging/ re-encoded, differently-named copies of DUPES (scratch files); the duplicate scenes move
                them into music/_Downloads
  config.json   scratch config for serve.py

The source defaults to ~/Music/ALAC (PITCH_SOURCE_MUSIC to change). Names below are "Artist/File.m4a" relative
to it; any that don't exist in your library are skipped, so edit them to taste.

SAFETY: the scenes must never call an endpoint that writes tags or moves/deletes files on a symlinked track."""
import json
import os
import subprocess
import sys

from paths import WORK

SOURCE = os.path.expanduser(os.environ.get("PITCH_SOURCE_MUSIC", "~/Music/ALAC"))
INBOX_TRACKS = [
    "Alphaville/Alphaville - Big in Japan.m4a",
    "Avicii/Avicii - Waiting For Love.m4a",
    "Beastie Boys/Beastie Boys - Fight For Your Right.m4a",
    "Anastacia/Anastacia - I'm Outta Love.m4a",
]
# (source track, scratch file name, mp3 bitrate, tags written to the copy)
DUPES = [
    ("ABBA/ABBA - Dancing Queen.m4a", "Track 07.mp3", "128k", {"title": "Track 07"}),
    ("Billie Eilish/Billie Eilish - bad guy.m4a", "billie_eilish_badguy_FINAL.mp3", "192k", {}),
    ("Bee Gees/Bee Gees - How Deep Is Your Love.m4a", "04 How Deep.mp3", "160k", {"artist": "BeeGees", "title": "How Deep"}),
    ("Avicii/Avicii - The Nights.m4a", "Avicii-TheNights(1).mp3", "128k", {"artist": "Avicii", "title": "The Nights (copy)"}),
]


def build():
    music, inbox, staging = (os.path.join(WORK, d) for d in ("music", "inbox", "dupes_staging"))
    if os.path.isdir(music):
        print("music tree already exists:", music)
        return
    for d in (music, inbox, staging):
        os.makedirs(d)
    held_back = {rel for rel in INBOX_TRACKS if os.path.isfile(os.path.join(SOURCE, rel))}
    n = 0
    for artist in sorted(os.listdir(SOURCE)):
        folder = os.path.join(SOURCE, artist)
        if not os.path.isdir(folder):
            continue
        os.makedirs(os.path.join(music, artist), exist_ok=True)
        for name in sorted(os.listdir(folder)):
            if f"{artist}/{name}" in held_back or name.startswith("._") or not name.lower().endswith((".m4a", ".mp3", ".flac")):
                continue
            os.symlink(os.path.join(folder, name), os.path.join(music, artist, name))
            n += 1
    for rel in held_back:
        os.symlink(os.path.join(SOURCE, rel), os.path.join(inbox, os.path.basename(rel)))
    for src, out_name, bitrate, tags in DUPES:
        path = os.path.join(SOURCE, src)
        if not os.path.isfile(path):
            print("skipping missing duplicate source:", src)
            continue
        meta = [x for k, v in tags.items() for x in ("-metadata", f"{k}={v}")]
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", path, "-vn", "-map_metadata", "-1", "-c:a", "libmp3lame",
                        "-b:a", bitrate, "-t", "170", *meta, os.path.join(staging, out_name)], check=True)
    json.dump({"music_dir": music, "theme": "default", "layoutMode": "classic"}, open(os.path.join(WORK, "config.json"), "w"))
    # brand assets the cards use
    subprocess.run(["sips", "-s", "format", "png", os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "assets", "AppIcon.icns"),
                    "--out", os.path.join(WORK, "icon.png")], capture_output=True)
    font = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "static", "fonts", "Oswald.ttf")
    subprocess.run(["cp", font, os.path.join(WORK, "Oswald.ttf")])
    print(f"demo library: {n} symlinked tracks, {len(held_back)} held back for the import scene, {len(os.listdir(staging))} duplicates staged")


if __name__ == "__main__":
    build()
