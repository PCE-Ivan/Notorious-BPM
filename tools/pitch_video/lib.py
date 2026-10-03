"""Shared helpers for the pitch scenes: a per-beat recorder that remembers how
its frames should be laid out on the timeline, plus app-state helpers."""
import asyncio
import json
import os
import urllib.parse
import urllib.request

import cap

HERE = cap.HERE
MANIFEST_DIR = os.path.join(HERE, "manifests")
os.makedirs(MANIFEST_DIR, exist_ok=True)
GLIDE_FPS = 22


def api(path, method="GET", body=None):
    req = urllib.request.Request(cap.APP + path, method=method,
                                 data=(json.dumps(body).encode() if body is not None else None),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def find_track(artist, title_part):
    d = api("/api/tracks?limit=20&q=" + urllib.parse.quote(f"{artist} {title_part}"))
    for t in d["tracks"]:
        if (t["artist"] or "").lower() == artist.lower() and title_part.lower() in (t["title"] or "").lower():
            return t
    raise RuntimeError(f"track not found: {artist} / {title_part}")


class Rec:
    """Frames for one beat, in order. `hold` = a still whose time on screen
    stretches to fit the narration (weights share the leftover time); `seq` =
    numbered frames played at a fixed fps."""

    def __init__(self, beat):
        self.beat = beat
        self.segments = []

    async def hold(self, page, name, weight=1.0, zoom=None):
        await page.shot(name)
        self.segments.append({"t": "hold", "f": name, "w": weight, "zoom": zoom})

    async def hold_hi(self, page, name, weight=1.0, zoom=None):
        await page.shot_hi(name)
        self.segments.append({"t": "hold", "f": name, "w": weight, "zoom": zoom})

    def hold_existing(self, name, weight=1.0, zoom=None):
        self.segments.append({"t": "hold", "f": name, "w": weight, "zoom": zoom})

    def seq(self, prefix, fps):
        self.segments.append({"t": "seq", "prefix": prefix, "fps": fps})

    async def click(self, page, selector, action_js, prefix, frames=7, settle=0.3):
        await cap.click_beat(page, selector, action_js, prefix, frames=frames, settle=settle)
        self.seq(prefix, GLIDE_FPS)

    async def glide(self, page, selector, prefix, frames=7):
        await cap.glide(page, selector, frames, prefix)
        self.seq(prefix, GLIDE_FPS)

    def save(self):
        with open(os.path.join(MANIFEST_DIR, f"{self.beat}.json"), "w") as f:
            json.dump(self.segments, f, indent=1)


SCRATCH_ONLY_NOTE = """SAFETY: the demo library's tracks are symlinks to the user's real files. Never call an endpoint that
writes tags or moves/deletes files on them (/api/tags, organize, delete, convert...). Tag edits are only for the
scratch re-encoded copies under music/_Downloads."""


async def fresh(page, theme="default", dark=False):
    api("/api/theme", "POST", {"theme": theme})   # the server-saved theme wins over localStorage
    api("/api/layout-mode", "POST", {"layoutMode": "classic"})
    await page.nav(cap.APP, wait=0.4)
    await page.js(f"localStorage.setItem('jukebox-welcome-seen','1'); localStorage.setItem('jukebox-theme','{theme}'); return 1;")
    await page.call("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-color-scheme", "value": "dark" if dark else "light"}]})
    await page.nav(cap.APP, wait=1.6)
    await cap.overlays(page)
    await page.js("""
      document.getElementById('current-folder').textContent = '~/Music/ALAC';
      document.getElementById('current-folder').title = '';
      return 1;""")
    await asyncio.sleep(0.3)


async def set_theme(page, name):
    await page.js(f"""
      const s = document.getElementById('theme-select');
      s.value = {json.dumps(name)}; s.dispatchEvent(new Event('change', {{bubbles: true}}));
      return 1;""", wait=0.5)


FAKE_AUDIO = """
// The real <audio> element is never given a source here (headless Chrome wedges on local audio),
// so the page's own track-change rendering is fed a track and a pretend playhead instead.
window.__t = %(t)s;
Object.defineProperty(audio, 'src', {get: () => location.origin + '/api/stream/%(id)s', configurable: true});
Object.defineProperty(audio, 'duration', {get: () => 211, configurable: true});
Object.defineProperty(audio, 'currentTime', {get: () => window.__t, set: (v) => {}, configurable: true});
"""


async def fake_play(page, track, progress=0.35):
    t = json.dumps(track)
    await page.js(FAKE_AUDIO % {"t": 211 * progress, "id": track["id"]} + f"""
      const tr = {t};
      state.currentTrack = tr;
      document.getElementById('np-title').textContent = tr.title;
      document.getElementById('np-sub').textContent = [tr.artist, tr.primary_genre, tr.year].filter(Boolean).join(' · ');
      const art = document.getElementById('np-art'); art.classList.remove('hidden'); art.src = artUrl(tr.id, true);
      ['play-pause','hifi-play-pause','cassette-play-pause','vinyl-play-pause'].forEach(id => {{ const e = document.getElementById(id); if (e) e.textContent = '⏸'; }});
      themeOnTrackChange(tr);
      updateStars(tr.rating || 0);
      ['seek','hifi-seek','cassette-seek','vinyl-seek'].forEach(id => {{ const e = document.getElementById(id); if (e) {{ e.value = {progress * 100}; e.style.setProperty('--fill', '{progress * 100}%'); }} }});
      document.getElementById('time-display').textContent = '1:14 / 3:31';
      try {{ refreshPlayingHighlight(); }} catch (e) {{}}
      return 1;""", wait=0.5)


MOTION_TICK = """
const k = %(k)s;                    // frame index
const deg = k * %(step)s;
const disc = document.getElementById('vinyl-disc');
if (disc) disc.style.transform = 'rotate(' + deg + 'deg)';
window.__t = %(t0)s + k * %(dt)s;   // pretend playhead advances: the tonearm walks inward
try { updateVinylTonearm(); } catch (e) {}
// A needle held in a moderate band with phrase-length swells, like real programme material.
const lv = (ph) => Math.min(0.95, Math.max(0.05, 0.52 + Math.sin(deg / 95 + ph) * 0.17 + Math.sin(deg / 31 + ph * 2.3) * 0.12 + Math.sin(deg / 11 + ph * 3.1) * 0.06));
const nl = document.getElementById('vu-needle-l'), nr = document.getElementById('vu-needle-r');
if (nl) nl.style.transform = 'rotate(' + (-60 + lv(0.4) * 96) + 'deg)';
if (nr) nr.style.transform = 'rotate(' + (-60 + lv(1.7) * 96) + 'deg)';
const rl = document.getElementById('reel-left'), rr = document.getElementById('reel-right');
if (rl) rl.style.transform = 'rotate(' + (deg * 0.75) + 'deg)';
if (rr) rr.style.transform = 'rotate(' + (deg * 1.25) + 'deg)';
// cassette spectrum bars: bass-heavy, shimmering
document.querySelectorAll('#cassette-visualizer .cas-vis-bar').forEach((b, i, all) => {
  const slope = 1 - (i / all.length) * 0.55;
  const v = slope * (0.5 + 0.38 * Math.sin(deg / 8 + i * 0.9) + 0.12 * Math.sin(deg / 3.1 + i * 1.7));
  b.style.height = Math.max(6, Math.min(100, v * 100)) + '%%';
});
return 1;
"""


async def motion(page, prefix, frames, step=5.0, t0=60.0, dt=0.12, quality=88):
    for k in range(frames):
        await page.js(MOTION_TICK % {"k": k, "step": step, "t0": t0, "dt": dt})
        await page.shot(f"{prefix}_{k:03d}", quality)
