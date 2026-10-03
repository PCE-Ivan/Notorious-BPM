"""The scratch app server the scenes drive: port 5788, a copy-only library under the work folder.
Never points at the real library or config."""
import os
import sys

from paths import REPO, WORK

os.environ["JUKEBOX_DB_PATH"] = os.path.join(WORK, "library.db")
os.environ["JUKEBOX_CONFIG_PATH"] = os.path.join(WORK, "config.json")
os.environ["JUKEBOX_MUSIC_DIR"] = os.path.join(WORK, "music")
os.environ["JUKEBOX_STATIC_DIR"] = os.path.join(REPO, "static")
sys.path.insert(0, REPO)
import app  # noqa: E402

app.app.run(host="127.0.0.1", port=5788, debug=False, use_reloader=False, threaded=True)
