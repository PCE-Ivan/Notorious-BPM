"""Frame capture for the pitch video: drives a headless Chrome over its own
DevTools websocket (never the real screen) and writes JPEG frames at 1920x1080
(a 1440x810 CSS-pixel viewport at device scale 4/3).

Every call has a timeout; a scene that wedges raises and is re-run by run.py
against a fresh Chrome. Scenes are independent: each starts by navigating."""
import asyncio
import base64
import json
import os
import subprocess
import time
import urllib.request

import websockets

from paths import WORK as HERE  # generated files live in the work folder
APP = "http://127.0.0.1:5788"
PORT = 9444
W, H, DSF = 1440, 810, 4 / 3
FRAMES = os.path.join(HERE, "frames")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def start_chrome():
    stop_chrome()
    profile = os.path.join(HERE, "chrome_profile")
    subprocess.run(["rm", "-rf", profile])
    subprocess.Popen(
        [CHROME, "--headless=new", "--disable-gpu", f"--remote-debugging-port={PORT}",
         f"--window-size={W},{H}", f"--user-data-dir={profile}", "--mute-audio",
         "--hide-scrollbars", "--no-first-run", "--disable-background-timer-throttling",
         "--disable-renderer-backgrounding", "about:blank"],
        stdout=open(os.path.join(HERE, "chrome.log"), "w"), stderr=subprocess.STDOUT,
    )
    for _ in range(40):
        time.sleep(0.5)
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=2).read()
            return
        except Exception:
            pass
    raise RuntimeError("Chrome didn't start")


def stop_chrome():
    subprocess.run(["pkill", "-9", "-f", f"remote-debugging-port={PORT}"], capture_output=True)
    time.sleep(0.5)


class Page:
    def __init__(self, ws):
        self.ws = ws
        self.n = 0
        self.frame_no = {}

    @classmethod
    async def open(cls):
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=5) as r:
            targets = json.loads(r.read())
        url = next(t["webSocketDebuggerUrl"] for t in targets if t.get("type") == "page")
        ws = await websockets.connect(url, max_size=80 * 1024 * 1024)
        page = cls(ws)
        await page.call("Page.enable")
        await page.call("Runtime.enable")
        await page.viewport(W, H)
        return page

    async def call(self, method, params=None, timeout=25):
        self.n += 1
        mid = self.n
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))

        async def wait():
            while True:
                data = json.loads(await self.ws.recv())
                if data.get("id") == mid:
                    return data
        data = await asyncio.wait_for(wait(), timeout)
        if "error" in data:
            raise RuntimeError(f"{method}: {data['error']}")
        return data.get("result", {})

    async def viewport(self, w, h):
        await self.call("Emulation.setDeviceMetricsOverride",
                        {"width": w, "height": h, "deviceScaleFactor": DSF, "mobile": False})

    async def nav(self, url, wait=0.8):
        await self.call("Page.navigate", {"url": url})
        await asyncio.sleep(wait)

    async def js(self, code, wait=0.0, timeout=25):
        r = await self.call("Runtime.evaluate", {
            "expression": f"(async () => {{\n{code}\n}})()", "awaitPromise": True, "returnByValue": True,
        }, timeout=timeout)
        if r.get("exceptionDetails"):
            raise RuntimeError("JS: " + json.dumps(r["exceptionDetails"])[:400])
        if wait:
            await asyncio.sleep(wait)
        return r.get("result", {}).get("value")

    async def shot(self, name, quality=92):
        os.makedirs(FRAMES, exist_ok=True)
        r = await self.call("Page.captureScreenshot", {"format": "jpeg", "quality": quality})
        path = os.path.join(FRAMES, name + ".jpg")
        with open(path, "wb") as f:
            f.write(base64.b64decode(r["data"]))
        return path

    async def shot_hi(self, name, dsf=2.6667, quality=90):
        """A larger capture of the same layout, for beats that crop in on a corner."""
        await self.call("Emulation.setDeviceMetricsOverride", {"width": W, "height": H, "deviceScaleFactor": dsf, "mobile": False})
        await asyncio.sleep(0.25)
        path = await self.shot(name, quality)
        await self.viewport(W, H)
        await asyncio.sleep(0.2)
        return path

    async def seq(self, prefix, n, tick=None, quality=90):
        """n numbered frames; tick(i) (async) runs before each one."""
        for i in range(n):
            if tick:
                await tick(i)
            await self.shot(f"{prefix}_{i:03d}", quality)


# ------------------------------------------------------------------ overlays --
OVERLAY_JS = r"""
if (!window.__pitch) {
  const css = document.createElement('style');
  css.textContent = `
    #__cursor { position: fixed; left: 0; top: 0; width: 34px; height: 34px; z-index: 2147483000; pointer-events: none;
      transform: translate(-6px, -4px); filter: drop-shadow(0 2px 3px rgba(0,0,0,.45)); }
    .__ripple { position: fixed; width: 14px; height: 14px; border-radius: 50%; border: 3px solid #2D89EF; z-index: 2147482990;
      pointer-events: none; transform: translate(-50%, -50%); animation: __rip .55s ease-out forwards; }
    @keyframes __rip { from { width: 14px; height: 14px; opacity: 1; } to { width: 84px; height: 84px; opacity: 0; } }
    .__target { outline: 3px solid #2D89EF !important; outline-offset: 3px !important; box-shadow: 0 0 0 8px rgba(45,137,239,.28) !important; }
    #__caption { position: fixed; left: 40px; bottom: 74px; z-index: 2147482900; pointer-events: none; display: flex; align-items: stretch;
      transform: translateY(0); opacity: 1; }
    #__caption.center { left: 50%; right: auto; transform: translateX(-50%); bottom: 112px; }
    #__caption.right { left: auto; right: 40px; }
    #__caption.top { bottom: auto; top: 300px; }
    #__caption .bar { width: 6px; background: #2D89EF; border-radius: 3px; margin-right: 14px; }
    #__caption .txt { background: rgba(14,15,17,.86); color: #fff; font: 600 30px/1.2 "Oswald", "Futura", sans-serif; letter-spacing: .01em;
      padding: 12px 22px 12px 4px; border-radius: 0 8px 8px 0; box-shadow: 0 8px 30px rgba(0,0,0,.35); }
    #__caption .sub { display: block; font: 400 18px/1.35 -apple-system, "Segoe UI", sans-serif; color: #c9ced6; margin-top: 3px; letter-spacing: 0; }
    #__keys { position: fixed; left: 50%; bottom: 120px; transform: translateX(-50%); z-index: 2147482950; display: flex; gap: 12px; pointer-events: none; }
    .__key { min-width: 64px; height: 64px; padding: 0 16px; display: flex; align-items: center; justify-content: center;
      font: 600 30px/1 ui-monospace, Menlo, monospace; color: #111; background: linear-gradient(#fafafa, #d9d9dc); border-radius: 10px;
      border: 1px solid #8d8d92; border-bottom-width: 5px; box-shadow: 0 10px 24px rgba(0,0,0,.4); }
  `;
  document.head.appendChild(css);
  const cur = document.createElement('div');
  cur.id = '__cursor';
  cur.innerHTML = '<svg width="34" height="34" viewBox="0 0 24 24"><path d="M4 2 L4 19 L8.4 15 L11.4 21.6 L14 20.4 L11 14 L17 14 Z" fill="#fff" stroke="#111" stroke-width="1.5" stroke-linejoin="round"/></svg>';
  document.body.appendChild(cur);
  window.__cx = 760; window.__cy = 420;
  cur.style.left = window.__cx + 'px'; cur.style.top = window.__cy + 'px';
  window.__pitch = {
    place(x, y) { window.__cx = x; window.__cy = y; const c = document.getElementById('__cursor'); c.style.left = x + 'px'; c.style.top = y + 'px'; },
    center(sel) { const e = document.querySelector(sel); if (!e) return null; const r = e.getBoundingClientRect(); return [r.left + Math.min(r.width / 2, 120), r.top + r.height / 2]; },
    ripple() { const r = document.createElement('div'); r.className = '__ripple'; r.style.left = window.__cx + 'px'; r.style.top = window.__cy + 'px';
      document.body.appendChild(r); setTimeout(() => r.remove(), 650); },
    caption(title, sub, pos) { let c = document.getElementById('__caption'); if (c) c.remove();
      if (!title) return; c = document.createElement('div'); c.id = '__caption'; if (pos) c.className = pos;
      c.innerHTML = '<div class="bar"></div><div class="txt">' + title + (sub ? '<span class="sub">' + sub + '</span>' : '') + '</div>'; document.body.appendChild(c); },
    keys(list) { let k = document.getElementById('__keys'); if (k) k.remove(); if (!list || !list.length) return;
      k = document.createElement('div'); k.id = '__keys'; k.innerHTML = list.map(x => '<div class="__key">' + x + '</div>').join(''); document.body.appendChild(k); },
  };
}
"ok"
"""


async def overlays(page):
    await page.js(OVERLAY_JS)


async def glide(page, selector, frames=7, prefix=None, hold=1):
    """Moves the synthetic cursor to `selector` in eased steps; captures a
    frame per step when `prefix` is given. Returns the target's centre."""
    pos = await page.js(f"return window.__pitch.center({json.dumps(selector)});")
    if not pos:
        raise RuntimeError(f"no target {selector}")
    sx, sy = await page.js("return [window.__cx, window.__cy];")
    for i in range(1, frames + 1):
        t = i / frames
        e = 1 - (1 - t) ** 3  # ease-out
        x, y = sx + (pos[0] - sx) * e, sy + (pos[1] - sy) * e
        await page.js(f"window.__pitch.place({x}, {y}); return 1;")
        if prefix:
            await page.shot(f"{prefix}_a{i:02d}")
    return pos


async def click_beat(page, selector, action_js, prefix, frames=7, settle=0.25):
    """Glide -> highlight + ripple (1-2 frames) -> run the action."""
    await glide(page, selector, frames, prefix)
    await page.js(f"var e = document.querySelector({json.dumps(selector)}); if (e) e.classList.add('__target'); window.__pitch.ripple(); return 1;")
    await page.shot(f"{prefix}_b00")
    await asyncio.sleep(0.12)
    await page.shot(f"{prefix}_b01")
    await page.js(action_js, wait=settle)
    await page.js(f"var e = document.querySelector({json.dumps(selector)}); if (e) e.classList.remove('__target'); return 1;")
