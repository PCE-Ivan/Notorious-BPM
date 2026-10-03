"""One async function per pitch scene. Each records its beats' frames into
manifests/<beat>.json (see lib.Rec); narration timing is applied later."""
import asyncio
import json

import cap
import lib
from lib import Rec, find_track

SHOWCASE = ("Billie Eilish", "bad guy")


async def caption(page, title, sub=None, pos=None):
    await page.js(f"window.__pitch.caption({json.dumps(title)}, {json.dumps(sub)}, {json.dumps(pos)}); return 1;")


async def scene_library(page):
    """Tracks -> Albums -> Artists."""
    r = Rec("library")
    await lib.fresh(page)
    total = lib.api("/api/facets")["total"]   # the first scan of this library: 1,515 tracks in 10.9 s
    await caption(page, f"{total:,} tracks indexed in 11 seconds", "Cover art, genre, decade, language — read from your files")
    await r.hold(page, "library_tracks", weight=3.0, zoom="in")
    await caption(page, None)
    await r.click(page, '.view-tab[data-view="albums"]', "setBrowseView('albums');", "library_toalbums")
    await page.js("await new Promise(r => setTimeout(r, 900)); return 1;")
    await caption(page, "Browse by album", "Every cover, one scroll")
    await r.hold(page, "library_albums", weight=3.0, zoom="in")
    await caption(page, None)
    await r.click(page, '.view-tab[data-view="artists"]', "setBrowseView('artists');", "library_toartists")
    await page.js("await new Promise(r => setTimeout(r, 900)); return 1;")
    await caption(page, "…or by artist")
    await r.hold(page, "library_artists", weight=2.2, zoom="in")
    r.save()


async def scene_search(page):
    r = Rec("search")
    await lib.fresh(page)
    await page.js("document.getElementById('search').focus(); return 1;")
    await r.glide(page, "#search", "search_in")
    await caption(page, "Search as you type", "Artist, title or album — instantly")
    text = "abba"
    for i, ch in enumerate(text, 1):
        await page.js(f"""
          const s = document.getElementById('search'); s.value = {json.dumps(text[:i])};
          s.dispatchEvent(new Event('input', {{bubbles: true}})); return 1;""", wait=0.45)
        await page.shot(f"search_type{i}")
        r.hold_existing(f"search_type{i}", weight=0.5 if i < len(text) else 3.0, zoom=None if i < len(text) else "in")
    r.save()


async def key(page, name, shift=False):
    await page.js(f"""
      document.body.dispatchEvent(new KeyboardEvent('keydown', {{key: {json.dumps(name)}, shiftKey: {str(shift).lower()}, bubbles: true}}));
      return 1;""", wait=0.18)


async def scene_keys(page):
    r = Rec("keys")
    await lib.fresh(page)
    await page.js("document.activeElement && document.activeElement.blur(); return 1;")
    await caption(page, "Keyboard-first", "J / K to move · Shift to select · 1–5 to rate")
    await page.js("document.getElementById('__cursor').style.display = 'none'; window.__pitch.keys(['J']); return 1;")
    for i in range(3):
        await key(page, "j")
        await page.shot(f"keys_j{i}")
        r.hold_existing(f"keys_j{i}", weight=0.55)
    await page.js("window.__pitch.keys(['⇧', 'J']); return 1;")
    for i in range(4):
        await key(page, "J", shift=True)
        await page.shot(f"keys_sj{i}")
        r.hold_existing(f"keys_sj{i}", weight=0.55)
    await page.js("window.__pitch.keys(['⇧', 'K']); return 1;")
    await key(page, "K", shift=True)
    await page.shot("keys_sk")
    r.hold_existing("keys_sk", weight=0.55)
    # rate the playing track with a number key
    tr = find_track(*SHOWCASE)
    await page.js("window.__pitch.keys([]); return 1;")
    await lib.fake_play(page, tr)
    await page.js("window.__pitch.keys(['4']); return 1;")
    await key(page, "4")
    await page.js("await new Promise(r => setTimeout(r, 250)); return 1;")
    await page.shot("keys_rate")
    r.hold_existing("keys_rate", weight=2.2)
    r.save()


async def scene_skins(page):
    r = Rec("skins")
    tr = find_track(*SHOWCASE)
    await lib.fresh(page)
    await page.js("document.activeElement && document.activeElement.blur(); return 1;")
    plan = [("hifi", "Hi-Fi receiver", "Live VU meters", 92), ("cassette", "Cassette deck", "Reels that really turn", 80), ("vinyl", "Vinyl turntable", "A tonearm that follows the song", 112)]
    for theme, title, sub, frames in plan:
        await r.click(page, "#theme-select", f"""
          const s = document.getElementById('theme-select'); s.value = '{theme}';
          s.dispatchEvent(new Event('change', {{bubbles: true}})); return 1;""", f"skins_{theme}_sw", frames=6, settle=0.6)
        await lib.fake_play(page, tr)
        await page.js("window.__pitch.place(1500, 700); return 1;")
        await caption(page, title, sub)
        await lib.motion(page, f"skins_{theme}", frames, step=5.0 if theme != "vinyl" else 6.0, t0=50.0, dt=0.5 if theme == "vinyl" else 0.1)
        r.seq(f"skins_{theme}", 20)
    r.save()


async def scene_airplay(page):
    r = Rec("airplay")
    tr = find_track(*SHOWCASE)
    await lib.fresh(page)
    await lib.fake_play(page, tr)
    # AirPlay is a WebKit-only API: in the real desktop app the button appears once a device is on the network.
    await page.js("document.getElementById('airplay-btn').classList.remove('hidden'); return 1;")
    await caption(page, "AirPlay to any speaker", "Stream to every room", "right")
    await r.click(page, "#airplay-btn", "return 1;", "airplay_btn", frames=9)
    await r.hold_hi(page, "airplay_hold", weight=1.6, zoom={"z0": 1.55, "z1": 1.75, "cx": 0.86, "cy": 0.96})
    await caption(page, "Volume leveling", "Quiet ballads and loud singles sound equally right", "right")
    await r.click(page, "#level-btn", "document.getElementById('level-btn').classList.add('active'); return 1;", "airplay_level", frames=8)
    await r.hold_hi(page, "airplay_hold2", weight=2.4, zoom={"z0": 1.75, "z1": 1.55, "cx": 0.82, "cy": 0.96})
    r.save()


async def scene_looks(page):
    r = Rec("looks")
    await lib.fresh(page, theme="system", dark=False)
    await page.js("setBrowseView('albums'); return 1;", wait=1.2)
    await caption(page, "Matches your Mac", "Light or dark — it follows the system")
    await r.hold(page, "looks_light", weight=1.6)
    await page.call("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-color-scheme", "value": "dark"}]})
    await page.js("await new Promise(r => setTimeout(r, 900)); return 1;")
    await caption(page, "Matches your Mac", "Light or dark — it follows the system")
    await r.hold(page, "looks_dark", weight=1.8)
    r.save()


async def scene_radio(page):
    r = Rec("radio")
    await lib.fresh(page)
    await caption(page, "Live radio, worldwide", "Thousands of stations — and a Shazam-style “identify this song”")
    await r.click(page, "#open-internet-radio", "document.getElementById('open-internet-radio').click(); return 1;", "radio_open", frames=8, settle=0.3)
    await page.js("await new Promise(r => setTimeout(r, 4500)); return 1;")
    await r.hold(page, "radio_list", weight=3.0, zoom="in")
    r.save()


async def scene_ipod(page):
    r = Rec("ipod")
    await lib.fresh(page)
    await caption(page, "Rescue an old iPod", "Copy, review, fix and import")
    await r.click(page, "#import-ipod", "document.getElementById('import-ipod').click(); return 1;", "ipod_open", frames=8, settle=0.7)
    await r.hold(page, "ipod_modal", weight=2.2)
    await page.js("document.getElementById('ipod-close').click(); return 1;", wait=0.4)
    # a selection, then the Convert dialog
    await page.js("""
      const cb = document.querySelectorAll('.track-row .col-check input')[8]; cb.click(); return 1;""", wait=0.5)
    await caption(page, "Convert to FLAC, ALAC or MP3", "Your files stay untouched")
    await r.click(page, "#selection-convert", "document.getElementById('selection-convert').click(); return 1;", "ipod_conv", frames=8, settle=0.8)
    await page.js("""
      document.querySelectorAll('.modal-backdrop:not(.hidden) *').forEach(n => { if (n.children.length === 0 && n.textContent.includes('/Users/')) n.textContent = n.textContent.replace(/\\/Users\\/[^/\\s]+/g, '~'); });
      return 1;""")
    await r.hold(page, "ipod_convert", weight=2.4)
    r.save()


# ------------------------------------------------------------------ phase B --
import os
import sqlite3
import time

from paths import WORK as PITCH


def prep_phase_b():
    """Puts the four scratch re-encodes back into the library, re-indexes, and forgets the fingerprints
    (so the sound-matching scene shows a genuine first run)."""
    stage, dest = os.path.join(PITCH, "dupes_staging"), os.path.join(PITCH, "music", "_Downloads")
    if os.path.isdir(stage) and not os.path.isdir(dest):
        os.rename(stage, dest)
    lib.api("/api/rescan", "POST", {})
    time.sleep(1)
    while lib.api("/api/scan-progress")["running"]:
        time.sleep(0.5)
    time.sleep(1)
    while lib.api("/api/art/warm/progress")["running"]:
        time.sleep(0.5)
    db = sqlite3.connect(os.path.join(PITCH, "library.db"))
    db.execute("DELETE FROM audio_fingerprints")
    db.commit()
    db.close()
    print("phase B ready:", lib.api("/api/facets")["total"], "tracks")


def clear_fingerprints():
    db = sqlite3.connect(os.path.join(PITCH, "library.db"))
    db.execute("DELETE FROM audio_fingerprints")
    db.commit()
    db.close()


async def scene_dups(page):
    r1, r2 = Rec("dup1"), Rec("dup2")
    clear_fingerprints()
    await lib.fresh(page)
    await caption(page, "Duplicates — found by sound", "Not by file name", "top")
    await r1.click(page, "#find-duplicates", "document.getElementById('find-duplicates').click(); return 1;", "dup_open", frames=8, settle=1.8)
    await r1.hold_hi(page, "dup_panel", weight=3.0, zoom={"z0": 1.0, "z1": 1.12, "cx": 0.5, "cy": 0.5})
    # the real thing: listen to the whole library
    await caption(page, None)
    await r2.click(page, "#dup-audio-scan", "document.getElementById('dup-audio-scan').click(); return 1;", "dup_go", frames=7, settle=0.5)
    marks = [0.10, 0.45, 0.85]
    got = 0
    for _ in range(600):
        st = lib.api("/api/audio-dupes/progress")
        frac = (st["done"] / st["total"]) if st.get("total") and st.get("stage") == "fingerprinting" else (1 if st.get("stage") == "comparing" or not st["running"] else 0)
        if got < len(marks) and frac >= marks[got]:
            await page.js("await new Promise(r => setTimeout(r, 600)); return 1;")
            await r2.hold_hi(page, f"dup_prog{got}", weight=0.9, zoom={"z0": 1.5, "z1": 1.56, "cx": 0.5, "cy": 0.74})
            got += 1
        if not st["running"]:
            break
        await asyncio.sleep(1.0)
    print("dup scan result:", lib.api("/api/audio-dupes/progress").get("result"))
    await page.js("await new Promise(r => setTimeout(r, 1500)); return 1;")
    await caption(page, "Same song. Different names, tags and formats.", None, "center")
    await r2.click(page, "#dup-audio-review", "document.getElementById('dup-audio-review').click(); return 1;", "dup_review_open", frames=7, settle=1.6)
    await r2.hold_hi(page, "dup_review", weight=3.0, zoom={"z0": 1.0, "z1": 1.1, "cx": 0.5, "cy": 0.4})
    await caption(page, "It marks the best copy", "Removed files go to the trash — never gone for good", "center")
    await page.js("document.querySelector('.dup-review-group').scrollIntoView({block: 'start'}); return 1;", wait=0.3)
    await r2.click(page, ".dup-review-group-actions button", "document.querySelector('.dup-review-group-actions button').click(); return 1;", "dup_best", frames=7, settle=0.5)
    await r2.hold_hi(page, "dup_selected", weight=3.2, zoom={"z0": 1.12, "z1": 1.0, "cx": 0.5, "cy": 0.45})
    r1.save()
    r2.save()


async def scene_tags(page):
    r = Rec("tags")
    await lib.fresh(page)
    await caption(page, "Every gap, found", "Missing genres, years and cover art", "center")
    await r.click(page, "#open-tag-checker", "document.getElementById('open-tag-checker').click(); return 1;", "tags_open", frames=8, settle=3.0)
    await r.hold_hi(page, "tags_panel", weight=3.0, zoom={"z0": 1.0, "z1": 1.1, "cx": 0.5, "cy": 0.38})
    r.save()


async def scene_undo(page):
    r = Rec("undo")
    # Tag edits are only ever made to the scratch re-encodes under _Downloads -- never to the (symlinked) real files.
    rows = [t for t in lib.api("/api/tracks?limit=50&q=Track%2007")["tracks"]]
    track = next(t for t in rows if "Track 07" in (t["title"] or ""))
    db = sqlite3.connect(os.path.join(PITCH, "library.db"))
    path = db.execute("SELECT path FROM tracks WHERE id = ?", (track["id"],)).fetchone()[0]
    db.close()
    assert path.startswith("_Downloads/") and not os.path.islink(os.path.join(PITCH, "music", path)), f"refusing to edit {path}"
    for field, value in (("artist", "ABBA"), ("title", "Dancing Queen")):
        lib.api(f"/api/tags/{track['id']}", "POST", {"field": field, "value": value, "batch": "pitch-demo"})
    await lib.fresh(page)
    await page.js("document.getElementById('open-history').scrollIntoView({block: 'center'}); return 1;", wait=0.3)
    await caption(page, "Every change can be undone", "Bulk edits and file moves are recorded")
    await r.click(page, "#open-history", "document.getElementById('open-history').click(); return 1;", "undo_open", frames=8, settle=1.0)
    await r.hold(page, "undo_list", weight=2.6)
    await r.click(page, ".history-undo", "document.querySelector('.history-undo').click(); return 1;", "undo_click", frames=7, settle=0.6)
    await r.hold(page, "undo_confirm", weight=1.6)
    await page.js("document.getElementById('confirm-ok').click(); return 1;", wait=2.0)
    await r.hold(page, "undo_done", weight=1.8)
    await page.js("document.getElementById('history-close').click(); return 1;", wait=0.3)
    await caption(page, "A health check keeps library and disk in step")
    await page.js("document.getElementById('open-health').scrollIntoView({block: 'center'}); return 1;", wait=0.2)
    await r.click(page, "#open-health", "document.getElementById('open-health').click(); return 1;", "health_open", frames=7, settle=2.5)
    await r.hold(page, "health_list", weight=2.4)
    r.save()


def reset_import():
    """Removes the scratch COPIES made by an earlier import run (never a symlink) and re-indexes."""
    inbox = os.path.join(PITCH, "inbox")
    music = os.path.join(PITCH, "music")
    for name in os.listdir(inbox):
        for root, _dirs, files in os.walk(music):
            if name in files:
                path = os.path.join(root, name)
                if not os.path.islink(path):
                    os.remove(path)
                    print("removed imported copy:", os.path.relpath(path, music))
    lib.api("/api/rescan", "POST", {"force_prune": True})
    time.sleep(1)
    while lib.api("/api/scan-progress")["running"]:
        time.sleep(0.5)


async def scene_import(page):
    r = Rec("import")
    reset_import()
    await lib.fresh(page)
    await caption(page, "Drop in files or whole folders", "Filed under the right artist · duplicates skipped")
    # the overlay the page shows while something is dragged over the window
    await page.js("""
      window.dispatchEvent(Object.assign(new Event('dragenter', {bubbles: true, cancelable: true}), {dataTransfer: {types: ['Files']}}));
      return 1;""", wait=0.5)
    await r.hold(page, "import_overlay", weight=2.4)
    # ...and the drop itself (in the desktop app the shell hands the real paths to this same route)
    inbox = os.path.join(PITCH, "inbox")
    await page.js("""
      window.pywebview = {api: {}};   // as in the desktop app, where the shell (not the page) receives the paths
      window.dispatchEvent(Object.assign(new Event('drop', {bubbles: true, cancelable: true}), {dataTransfer: {types: ['Files']}}));
      return 1;""", wait=0.1)
    lib.api("/api/import/files", "POST", {"paths": [inbox]})
    # wait for the real jobs: the copy, then the rescan that indexes what was copied
    while lib.api("/api/import/progress")["running"]:
        await asyncio.sleep(0.3)
    await page.js("await new Promise(r => setTimeout(r, 1700)); return 1;")
    await r.hold(page, "import_toast", weight=2.2)
    await asyncio.sleep(1.0)
    while lib.api("/api/scan-progress")["running"]:
        await asyncio.sleep(0.5)
    await page.js("await new Promise(r => setTimeout(r, 3500)); return 1;")   # the page refreshes itself
    await caption(page, "Added — with cover art", "Alphaville · Avicii · Beastie Boys · Anastacia")
    await page.js("""
      document.getElementById('search').value = 'big in japan';
      document.getElementById('search').dispatchEvent(new Event('input', {bubbles: true})); return 1;""", wait=1.6)
    await r.hold(page, "import_added", weight=2.6)
    r.save()


async def scene_thumb(page):
    """A clean (caption-free) vinyl frame for the thumbnail."""
    tr = find_track(*SHOWCASE)
    await lib.fresh(page, theme="vinyl")
    await page.js("document.getElementById('__cursor').style.display = 'none'; return 1;")
    await page.js("setBrowseView('albums'); return 1;", wait=1.2)
    await lib.fake_play(page, tr)
    await lib.motion(page, "thumb_vinyl", 1, step=6.0, t0=60.0, dt=0.5)
