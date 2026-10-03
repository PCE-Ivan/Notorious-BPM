"""run.py <scene> [<scene> ...]: runs scenes against a fresh Chrome, retrying a wedged one."""
import asyncio
import sys

import cap
import scenes


async def one(name):
    fn = getattr(scenes, "scene_" + name)
    cap.start_chrome()
    page = await cap.Page.open()
    try:
        await fn(page)
    finally:
        try:
            await page.ws.close()
        except Exception:
            pass


def run_scenes(names):
    for name in names:
        for attempt in (1, 2, 3):
            try:
                asyncio.run(asyncio.wait_for(one(name), 600))
                print(f"scene {name}: ok", flush=True)
                break
            except Exception as e:
                print(f"scene {name} attempt {attempt} failed: {type(e).__name__}: {e}", flush=True)
        else:
            print(f"scene {name}: GAVE UP", flush=True)
    cap.stop_chrome()


if __name__ == "__main__":
    run_scenes(sys.argv[1:])
