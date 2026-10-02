"""Application logging -- a rotating log file plus uncaught-exception hooks.

A double-clicked .app has no visible stderr, so before this a failure (the
macOS Desktop-permission block that kept returning a bare "500 Internal
Server Error" is the example that made this necessary) left nothing to
read. Everything that goes wrong now lands in one file the user can open
from About -> "Show log", and the same text is bundled by /api/diagnostics.

Called once, at the very top of app.py -- deliberately *before*
library_manager.resolve_startup(), which sets JUKEBOX_DB_PATH to the real
current library: log_dir() treats a JUKEBOX_DB_PATH that's already set at
this point as a dev/test override and keeps the log next to that scratch
DB instead of in the real app-data folder, so a test run never writes into
(or reads from) a real installation's logs.
"""
import logging
import logging.handlers
import os
import sys
import threading

LOG_FILENAME = "notorious-bpm.log"
_configured = False
_log_path = None


def log_dir():
    override = os.environ.get("JUKEBOX_LOG_DIR")
    if override:
        return override
    # A scratch/test environment sets JUKEBOX_CONFIG_PATH to its own folder and
    # gets its logs there. Not keyed off JUKEBOX_DB_PATH: the real app sets that
    # too (to the open library, wherever the user saved it -- the Desktop, an
    # external drive), and logs belong in the app's own data folder, not
    # sprinkled next to a library file.
    config_override = os.environ.get("JUKEBOX_CONFIG_PATH")
    if config_override:
        return os.path.join(os.path.dirname(config_override), "logs")
    import config as jukebox_config
    return os.path.join(jukebox_config.get_app_data_dir(), "logs")


def log_path():
    return _log_path


def setup_logging():
    global _configured, _log_path
    if _configured:
        return _log_path
    _configured = True

    directory = log_dir()
    os.makedirs(directory, exist_ok=True)
    _log_path = os.path.join(directory, LOG_FILENAME)

    root = logging.getLogger("jukebox")
    root.setLevel(logging.INFO)
    root.propagate = False
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    file_handler = logging.handlers.RotatingFileHandler(
        _log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # Useful when running from a terminal; harmless when frozen (no console).
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    log = logging.getLogger("jukebox.crash")

    def _excepthook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("Uncaught exception", exc_info=(exc_type, exc, tb))

    def _thread_excepthook(args):
        if args.exc_type is SystemExit:
            return
        log.critical(
            "Uncaught exception in thread %s", getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook

    logging.getLogger("jukebox").info("Logging started -> %s", _log_path)
    return _log_path


def tail(max_bytes=40_000):
    """Last chunk of the current log file, for diagnostics."""
    if not _log_path or not os.path.isfile(_log_path):
        return ""
    with open(_log_path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        return f.read().decode("utf-8", errors="replace")
