import asyncio, os
import cap, cards
HERE = cap.HERE

async def main():
    ids = cards.cover_ids(84)
    body = f"""
    <div style="position:absolute;left:70px;top:150px;width:700px">
      <div class=big style="font-size:40px;letter-spacing:.16em;color:#f0a020;margin-bottom:14px">NOTORIOUS B.P.M.</div>
      <h1 style="font-size:148px;line-height:.96">FIX YOUR<br>MUSIC<br><span class=accent>LIBRARY.</span></h1>
      <div style="margin-top:26px;font:600 30px -apple-system,sans-serif;color:#d5d9df">Duplicates found by sound · Undo · Free</div>
    </div>
    <img src="file://{HERE}/frames/thumb_vinyl_000.jpg" style="position:absolute;right:-60px;top:110px;width:780px;border-radius:14px;
      box-shadow:0 30px 80px rgba(0,0,0,.7), 0 0 0 3px rgba(255,255,255,.12);transform:rotate(-3.5deg)">
    """
    html = cards.page(body, ids=ids, collage_opacity=.22, blur=2,
                      shade="linear-gradient(90deg, rgba(12,13,15,.88) 0%, rgba(12,13,15,.6) 55%, rgba(12,13,15,.35) 100%)")
    path = os.path.join(HERE, "cards", "thumb.html")
    open(path, "w").write(html)
    cap.start_chrome()
    p = await cap.Page.open()
    await p.call("Emulation.setDeviceMetricsOverride", {"width": 1440, "height": 810, "deviceScaleFactor": 1280 / 1440, "mobile": False})
    await p.nav("file://" + path, wait=2.0)
    import base64
    r = await p.call("Page.captureScreenshot", {"format": "jpeg", "quality": 95})
    os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
    open(os.path.join(HERE, "out", "thumbnail.jpg"), "wb").write(base64.b64decode(r["data"]))
    await p.ws.close()
    cap.stop_chrome()
if __name__ == "__main__":
    asyncio.run(main())
