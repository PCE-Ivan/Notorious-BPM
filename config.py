"""Shared config helpers for Jukebox — stores the music folder location so it
isn't hardcoded, letting it be set (or changed) via the Setup app."""
import json
import os
import sys
import tempfile
import threading

# Serializes every config read-modify-write cycle within this process.
# Flask's dev server handles requests on separate threads, and the theme
# and wood-finish selects each POST their own change immediately on every
# page load -- without this, two of those read-modify-write cycles
# overlapping could each read the same starting config, then each save
# back a version missing the other's change (last write wins, silently
# discarding it). See update_config() below, which every caller should
# use instead of load_config()+save_config() for anything read-modify-write.
_lock = threading.Lock()


def get_app_data_dir():
    """Per-OS location for Jukebox's own data (config, index, caches, trash,
    backups, logs) -- kept in one place so every module agrees on it without
    each hardcoding a macOS-only path."""
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    elif sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser(os.path.join("~", "AppData", "Roaming"))
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser(os.path.join("~", ".local", "share"))
    path = os.path.join(base, "Jukebox")
    os.makedirs(path, exist_ok=True)
    return path


CONFIG_PATH = os.path.join(get_app_data_dir(), "config.json")


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(cfg):
    """Written atomically (unique temp file + rename) so a save can never be
    observed mid-write by a concurrent reader -- a reader catching a
    half-written file would otherwise see corrupt JSON, fall back to {} in
    load_config(), and (if it then goes on to write itself) save that empty
    dict back, silently discarding music_dir and every other previously-
    saved key. The temp filename is unique per call (not a fixed
    CONFIG_PATH + ".tmp") so two overlapping saves can't collide on the
    same temp file and have one's os.replace() find it already consumed by
    the other. Prefer update_config() below over calling this directly --
    it also closes the separate lost-update race across two full
    read-modify-write cycles, which a unique temp file alone doesn't."""
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(CONFIG_PATH), prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp_path, CONFIG_PATH)
    except BaseException:
        os.unlink(tmp_path)
        raise


def update_config(mutate):
    """Read-modify-write a config change under a single lock -- e.g.
    update_config(lambda cfg: cfg.__setitem__("theme", name)). Use this
    instead of load_config()+save_config() for any change that depends on
    the config's current contents, so two such changes made around the
    same time (the theme and wood-finish selects both POST on every page
    load) can't each read a stale snapshot and overwrite each other."""
    with _lock:
        cfg = load_config()
        mutate(cfg)
        save_config(cfg)
        return cfg


def get_music_dir():
    """Resolution order: JUKEBOX_MUSIC_DIR env var (dev override) > saved config."""
    env = os.environ.get("JUKEBOX_MUSIC_DIR")
    if env:
        return env
    return load_config().get("music_dir") or None
