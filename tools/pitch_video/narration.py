import json
import os
import subprocess

from paths import WORK as HERE  # generated files live in the work folder
VOICE = "Daniel (Enhanced)"   # the voice approved for the first demo
RATE = 198                    # words per minute: brisk, still clear

NARRATION = [
    ("hook1", "You own thousands of songs."),
    ("hook2", "But the tags are a mess. The cover art is missing. And the same track is hiding in five different folders."),
    ("hook3", "Streaming services can't fix that. It isn't their library. It's yours."),
    ("title", "Meet Notorious B-P-M. The music library for the music you actually own."),
    ("library", "Point it at a folder, and it indexes everything: cover art, genre, decade, even language. Browse by track, by album, or by artist."),
    ("search", "Search as you type. Instantly. Even with twenty thousand songs."),
    ("keys", "Run it from the keyboard. J and K to move. Shift to select a range. One key to rate."),
    ("skins", "Then press play, and choose your player. A hi-fi receiver with live V U meters. A cassette deck with turning reels. Or a vinyl turntable, whose tonearm follows the song."),
    ("airplay", "Send it to any AirPlay speaker, and switch on volume leveling, so quiet ballads and loud singles sound equally right."),
    ("looks", "Light, dark, or matched to your Mac."),
    ("dup1", "Here's the clever part. Most duplicate finders compare file names. This one listens."),
    ("dup2", "It fingerprints the audio itself, so the same song is found even when the names, the tags, and the formats don't match. It marks the best copy, and anything you remove goes to the trash, not into thin air."),
    ("tags", "Missing genres, years, or cover art? The tag checker finds every gap, and can fill them in from online databases."),
    ("undo", "And you can always take it back. Every bulk edit and file move is recorded, with one-click undo. A health check keeps your library and your disk in step."),
    ("import", "Adding music is a drag away. Drop in files or whole folders, and they're filed under the right artist, with duplicates skipped."),
    ("ipod", "Rescue music from an old iPod. Convert to FLAC, A L A C, or M P 3."),
    ("radio", "Want something new? Thousands of live radio stations, and a Shazam-style button that names whatever's playing."),
    ("trust", "No account. No subscription. No cloud. It's free to use."),
    ("cta", "Notorious B-P-M. Get it free on GitHub, and take your music back."),
]

if __name__ == "__main__":
    out = os.path.join(HERE, "narration_audio")
    os.makedirs(out, exist_ok=True)
    durations = {}
    for key, text in NARRATION:
        aiff, wav = os.path.join(out, key + ".aiff"), os.path.join(out, key + ".wav")
        subprocess.run(["say", "-v", VOICE, "-r", str(RATE), "-o", aiff, text], check=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", aiff, "-ar", "44100", "-ac", "2", wav], check=True)
        d = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", wav],
                                 capture_output=True, text=True, check=True).stdout.strip())
        durations[key] = d
        os.remove(aiff)
        print(f"{key:8s} {d:5.2f}s  {len(text.split()):3d} words")
    json.dump(durations, open(os.path.join(HERE, "narration_durations.json"), "w"), indent=1)
    print("total narration: %.1fs" % sum(durations.values()))
