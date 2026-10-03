"""HTML cards for the pitch (hook, title, trust, call to action) and the YouTube thumbnail."""
import asyncio
import json
import os

import cap
import lib
from lib import Rec

from paths import WORK as HERE  # generated files live in the work folder


def cover_ids(n=84):
    d = lib.api("/api/albums?limit=300&sort=artist")
    ids = [a["art_id"] for a in d["items"] if a.get("art_id")]
    step = max(1, len(ids) // n)
    return ids[::step][:n]


def collage(ids, opacity, blur=0, cols=12):
    tiles = "".join(f'<img src="{cap.APP}/api/art/{i}?thumb=1">' for i in ids)
    return f'<div class="collage" style="opacity:{opacity};filter:blur({blur}px);grid-template-columns:repeat({cols},1fr)">{tiles}</div>'


BASE_CSS = """
@font-face { font-family: Oswald; src: url('file://%(here)s/Oswald.ttf'); font-weight: 200 700; }
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { width: 1440px; height: 810px; overflow: hidden; background: #0c0d0f; color: #fff;
  font-family: -apple-system, 'Helvetica Neue', sans-serif; }
.collage { position: absolute; inset: -20px; display: grid; gap: 4px; align-content: start; }
.collage img { width: 100%%; aspect-ratio: 1; object-fit: cover; display: block; }
.shade { position: absolute; inset: 0; }
.center { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; padding: 0 90px; }
h1, h2, .big { font-family: Oswald, 'Futura', sans-serif; font-weight: 700; letter-spacing: .005em; line-height: 1.02; }
.accent { color: #2D89EF; }
.amber { color: #f0a020; }
"""


def page(body, extra_css="", ids=None, collage_opacity=0.2, blur=0, shade="radial-gradient(ellipse at center, rgba(12,13,15,.55), rgba(12,13,15,.92))"):
    css = BASE_CSS % {"here": HERE} + extra_css
    bg = collage(ids or [], collage_opacity, blur) if ids else ""
    return f"<!doctype html><meta charset=utf-8><style>{css}</style>{bg}<div class=shade style=\"background:{shade}\"></div>{body}"


def build(ids):
    cards = {}
    cards["hook1"] = page(
        '<div class=center><div class=big style="font-size:46px;color:#9aa0a8;margin-bottom:10px;letter-spacing:.14em;text-transform:uppercase">You own</div>'
        '<h1 style="font-size:190px">THOUSANDS<br>OF SONGS.</h1></div>', ids=ids, collage_opacity=.34)
    mess = ["Track 07.mp3", "Unknown Artist - Untitled (2).mp3", "01 - 01 - 01.m4a", "billie_eilish_badguy_FINAL.mp3", "song(1)(1).mp3",
            "NEW FOLDER 3", "AUDIO_2019_final_v2.wav", "04 How Deep.mp3", "Avicii-TheNights(1).mp3", "untitled.m4a", "DJ_MIX_old/", "??? - ???.mp3"]
    spots = [(6, 10, -4), (52, 6, 3), (14, 34, 2), (58, 28, -3), (4, 58, -2), (46, 52, 4), (22, 80, -3), (62, 74, 2), (36, 20, 5), (74, 46, -4), (30, 66, 3), (70, 12, -2)]
    ghosts = "".join(f'<div class=ghost style="left:{x}%;top:{y}%;transform:rotate({r}deg)">{t}</div>' for t, (x, y, r) in zip(mess, spots))
    ghost_css = ".ghost { position:absolute; font: 500 26px ui-monospace, Menlo, monospace; color: rgba(255,255,255,.28); white-space: nowrap; }"
    def hook2(n):
        lines = [("The tags are a mess.", "#fff"), ("The cover art is missing.", "#fff"), ("The same track hides in five folders.", "#f0a020")]
        body = "".join(f'<h2 style="font-size:84px;margin:8px 0;color:{c};opacity:{1 if i < n else 0}">{t}</h2>' for i, (t, c) in enumerate(lines))
        return page(f"{ghosts}<div class=center>{body}</div>", ghost_css, ids=ids, collage_opacity=.12, blur=6)
    cards["hook2a"], cards["hook2b"], cards["hook2c"] = hook2(1), hook2(2), hook2(3)
    cards["hook3a"] = page('<div class=center><h1 style="font-size:96px;color:#cfd3d9">Streaming can\'t fix that.</h1></div>', ids=ids, collage_opacity=.16, blur=3)
    cards["hook3b"] = page('<div class=center><h1 style="font-size:96px;color:#8e949c">Streaming can\'t fix that.</h1>'
                           '<h1 style="font-size:150px;margin-top:26px">It isn\'t their library.<br><span class=accent>It\'s yours.</span></h1></div>', ids=ids, collage_opacity=.16, blur=3)
    cards["title"] = page(
        '<div class=center><img src="file://%s/icon.png" style="width:190px;height:190px;border-radius:42px;box-shadow:0 20px 60px rgba(0,0,0,.6);margin-bottom:34px">'
        '<h1 style="font-size:132px;letter-spacing:.02em">NOTORIOUS B.P.M.</h1>'
        '<div style="font:500 38px/1.3 -apple-system,sans-serif;color:#d5d9df;margin-top:22px">The music library for the music you <span class=accent style="font-weight:700">actually own</span>.</div></div>' % HERE,
        ids=ids, collage_opacity=.5, shade="radial-gradient(ellipse at center, rgba(12,13,15,.62), rgba(12,13,15,.9))")
    def trust(n):
        items = [("FREE", "Free to use", "#2D89EF"), ("PRIVATE", "No account · no subscription · no cloud", "#f0a020"), ("YOURS", "Your files, your folders, your rules", "#3ecf6e")]
        cells = "".join(
            f'<div style="opacity:{1 if i < n else 0};margin:0 36px"><div class=big style="font-size:118px;color:{c}">{h}</div>'
            f'<div style="font:500 26px/1.35 -apple-system,sans-serif;color:#cfd3d9;max-width:380px;margin-top:8px">{s}</div></div>' for i, (h, s, c) in enumerate(items))
        return page(f'<div class=center><div style="display:flex;align-items:flex-start;justify-content:center">{cells}</div></div>', ids=ids, collage_opacity=.14, blur=4)
    cards["trust1"], cards["trust2"], cards["trust3"] = trust(1), trust(2), trust(3)
    cards["cta"] = page(
        '<div class=center><img src="file://%s/icon.png" style="width:150px;height:150px;border-radius:34px;box-shadow:0 16px 50px rgba(0,0,0,.6);margin-bottom:26px">'
        '<h1 style="font-size:104px">GET IT FREE ON GITHUB</h1>'
        '<div style="margin-top:22px;font:600 54px ui-monospace,Menlo,monospace;color:#2D89EF;letter-spacing:-.01em">github.com/PCE-Ivan/Notorious-BPM</div>'
        '<div style="margin-top:34px;font:500 28px -apple-system,sans-serif;color:#cfd3d9">Notorious B.P.M. · macOS · Apple Silicon &nbsp;·&nbsp; donations welcome, never required</div></div>' % HERE,
        ids=ids, collage_opacity=.42, shade="radial-gradient(ellipse at center, rgba(12,13,15,.4), rgba(12,13,15,.88))")
    return cards


async def render(page_, cards):
    os.makedirs(os.path.join(HERE, "cards"), exist_ok=True)
    for name, html in cards.items():
        path = os.path.join(HERE, "cards", name + ".html")
        open(path, "w").write(html)
        await page_.nav("file://" + path, wait=1.6)
        await page_.shot("card_" + name, quality=94)
        print("card", name)


async def main():
    ids = cover_ids()
    cap.start_chrome()
    p = await cap.Page.open()
    await render(p, build(ids))
    await p.ws.close()
    cap.stop_chrome()


if __name__ == "__main__":
    asyncio.run(main())
