"""Builds the pitch video from captured frames + narration.

  python3 assemble.py            full build -> out/pitch_silent.mp4, out/pitch.mp4, out/pitch.srt
  python3 assemble.py --draft    same, at 960x540 and crf 28 (fast; for checking timing)

Beat timeline: narration starts LEAD seconds into its beat; the beat ends TAIL seconds after it. Still frames
("hold") stretch to fill whatever time the fixed-rate frame runs leave; beats cross-fade into each other."""
import glob
import json
import os
import re
import subprocess
import sys

from paths import WORK as HERE  # generated files live in the work folder
FRAMES = os.path.join(HERE, "frames")
MAN = os.path.join(HERE, "manifests")
OUT = os.path.join(HERE, "out")
CLIPS = os.path.join(OUT, "clips")
DRAFT = "--draft" in sys.argv
W, H = (960, 540) if DRAFT else (1920, 1080)
CRF = "28" if DRAFT else "17"
PRESET = "veryfast" if DRAFT else "medium"
FPS = 30
LEAD, TAIL = 0.45, 0.50

durations = json.load(open(os.path.join(HERE, "narration_durations.json")))
from narration import NARRATION  # noqa: E402

TEXT = dict(NARRATION)


def card(*steps):
    """Hold segments from card frames: (card_name, weight)."""
    return [{"t": "hold", "f": "card_" + n, "w": w, "zoom": "in"} for n, w in steps]


# (beat, segments or manifest, fade into the NEXT beat)
BEATS = [
    ("hook1", card(("hook1", 1)), 0.30),
    ("hook2", card(("hook2a", 1.0), ("hook2b", 1.0), ("hook2c", 1.5)), 0.30),
    ("hook3", card(("hook3a", 1.0), ("hook3b", 2.4)), 0.30),
    ("title", card(("title", 1)), 0.45),
    ("library", "library", 0.18),
    ("search", "search", 0.18),
    ("keys", "keys", 0.30),
    ("skins", "skins", 0.30),
    ("airplay", "airplay", 0.30),
    ("looks", "looks", 0.30),
    ("dup1", "dup1", 0.18),
    ("dup2", "dup2", 0.30),
    ("tags", "tags", 0.30),
    ("undo", "undo", 0.30),
    ("import", "import", 0.30),
    ("ipod", "ipod", 0.30),
    ("radio", "radio", 0.35),
    ("trust", card(("trust1", 1.0), ("trust2", 1.0), ("trust3", 1.6)), 0.40),
    ("cta", card(("cta", 1)), 0.0),
]
FPS_SEQ = {}


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + " ".join(cmd)[:300] + "\n" + r.stderr[-1200:])


def seq_files(prefix):
    pat = re.compile(rf"^{re.escape(prefix)}_[^_]+\.jpg$")
    return sorted(f for f in os.listdir(FRAMES) if pat.match(f))


def load_segments(spec):
    segs = spec if isinstance(spec, list) else json.load(open(os.path.join(MAN, spec + ".json")))
    out = []
    for s in segs:
        if s["t"] == "seq":
            files = seq_files(s["prefix"])
            if not files:
                raise RuntimeError(f"no frames for {s['prefix']}")
            out.append({"t": "seq", "files": files, "fps": s["fps"], "dur": len(files) / s["fps"]})
        else:
            out.append(dict(s))
    return out


def zoom_filter(zoom, dur, w_in):
    """Smooth push-in/out on a still: scale per frame, then crop around a focus point."""
    if not zoom:
        return f"scale={W}:{H}:flags=lanczos"
    if zoom == "in":
        zoom = {"z0": 1.0, "z1": 1.06, "cx": 0.5, "cy": 0.5}
    z0, z1, cx, cy = zoom["z0"], zoom["z1"], zoom["cx"], zoom["cy"]
    u = f"min(1,t/{dur:.3f})"
    z = f"({z0}+({z1}-{z0})*(3*{u}*{u}-2*{u}*{u}*{u}))"
    return (f"scale=w='trunc({W}*{z}/2)*2':h='trunc({H}*{z}/2)*2':eval=frame:flags=lanczos,"
            f"crop={W}:{H}:x='min(max(in_w*{cx}-{W / 2},0),in_w-{W})':y='min(max(in_h*{cy}-{H / 2},0),in_h-{H})'")


def build_beat(name, spec):
    segs = load_segments(spec)
    narr = durations[name]
    fixed = sum(s["dur"] for s in segs if s["t"] == "seq")
    holds = [s for s in segs if s["t"] == "hold"]
    wsum = sum(s["w"] for s in holds) or 1.0
    total = max(narr + LEAD + TAIL, fixed + 0.45 * len(holds) + 0.2)
    spare = total - fixed
    parts = []
    for i, s in enumerate(segs):
        clip = os.path.join(CLIPS, f"{name}_{i:02d}.mp4")
        if s["t"] == "seq":
            listing = os.path.join(CLIPS, f"{name}_{i:02d}.txt")
            with open(listing, "w") as f:
                for fn in s["files"]:
                    f.write(f"file '{os.path.join(FRAMES, fn)}'\nduration {1 / s['fps']:.5f}\n")
                f.write(f"file '{os.path.join(FRAMES, s['files'][-1])}'\n")
            run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", listing,
                 "-vf", f"scale={W}:{H}:flags=lanczos,fps={FPS},format=yuv420p", "-c:v", "libx264", "-crf", CRF, "-preset", PRESET, "-an", clip])
        else:
            dur = max(0.25, spare * s["w"] / wsum)
            src = os.path.join(FRAMES, s["f"] + ".jpg")
            run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-framerate", str(FPS), "-t", f"{dur:.3f}", "-i", src,
                 "-vf", zoom_filter(s.get("zoom"), dur, None) + ",format=yuv420p", "-c:v", "libx264", "-crf", CRF, "-preset", PRESET, "-an", clip])
        parts.append(clip)
    beat_clip = os.path.join(CLIPS, f"{name}.mp4")
    listing = os.path.join(CLIPS, f"{name}_all.txt")
    with open(listing, "w") as f:
        for p in parts:
            f.write(f"file '{p}'\n")
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", listing, "-c", "copy", beat_clip])
    return beat_clip, total


def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?:])\s+", text) if s.strip()]


def srt_time(t):
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def main():
    os.makedirs(CLIPS, exist_ok=True)
    clips, totals, fades = [], [], []
    for name, spec, fade in BEATS:
        clip, total = build_beat(name, spec)
        clips.append(clip)
        totals.append(total)
        fades.append(fade)
        print(f"beat {name:8s} {total:6.2f}s")

    # timeline
    starts, t = [], 0.0
    for i, total in enumerate(totals):
        starts.append(t)
        t += total - (fades[i] if i < len(totals) - 1 else 0.0)
    duration = t + 0.6
    print("video length: %.1fs" % duration)

    # video: chained cross-fades
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for c in clips:
        cmd += ["-i", c]
    graph, label = [], "[0:v]"
    for i in range(1, len(clips)):
        offset = starts[i]
        out = f"[v{i}]"
        graph.append(f"{label}[{i}:v]xfade=transition=fade:duration={fades[i - 1]}:offset={offset:.3f}{out}")
        label = out
    graph.append(f"{label}fade=t=in:st=0:d=0.4,fade=t=out:st={duration - 0.8:.3f}:d=0.8,format=yuv420p[vout]")
    silent = os.path.join(OUT, "pitch_silent.mp4")
    cmd += ["-filter_complex", ";".join(graph), "-map", "[vout]", "-r", str(FPS), "-c:v", "libx264", "-crf", CRF, "-preset", PRESET,
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", silent]
    run(cmd)

    # audio: narration at exact offsets
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    for name, _s, _f in BEATS:
        cmd += ["-i", os.path.join(HERE, "narration_audio", name + ".wav")]
    graph, labels = [], []
    for i, (name, _s, _f) in enumerate(BEATS):
        ms = int((starts[i] + LEAD) * 1000)
        graph.append(f"[{i}:a]adelay={ms}|{ms},volume=1.0[a{i}]")
        labels.append(f"[a{i}]")
    graph.append("".join(labels) + f"amix=inputs={len(labels)}:normalize=0,loudnorm=I=-16:TP=-1.5:LRA=9,aresample=48000,afade=t=out:st={duration - 0.8:.3f}:d=0.8,apad=whole_dur={duration:.3f}[aout]")
    narr_path = os.path.join(OUT, "narration.m4a")
    cmd += ["-filter_complex", ";".join(graph), "-map", "[aout]", "-t", f"{duration:.3f}", "-c:a", "aac", "-b:a", "192k", narr_path]
    run(cmd)

    final = os.path.join(OUT, "pitch_draft.mp4" if DRAFT else "pitch.mp4")
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", silent, "-i", narr_path, "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "copy",
         "-shortest", "-movflags", "+faststart", final])

    # captions
    lines, n = [], 0
    for i, (name, _s, _f) in enumerate(BEATS):
        start, dur = starts[i] + LEAD, durations[name]
        sents = sentences(TEXT[name])
        weights = [len(s) for s in sents]
        pos = start
        for s, wt in zip(sents, weights):
            span = dur * wt / sum(weights)
            n += 1
            shown = s.replace("B-P-M", "B.P.M.").replace("V U meters", "VU meters").replace("M P 3", "MP3").replace("A L A C", "ALAC")
            lines.append(f"{n}\n{srt_time(pos)} --> {srt_time(pos + span)}\n{shown}\n")
            pos += span
    open(os.path.join(OUT, "pitch.srt"), "w").write("\n".join(lines))
    json.dump({"starts": starts, "totals": totals, "duration": duration, "beats": [b[0] for b in BEATS]},
              open(os.path.join(OUT, "timeline.json"), "w"), indent=1)
    print("done:", final)


if __name__ == "__main__":
    main()
