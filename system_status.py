"""Turns "the library won't open" into something a person can act on.

Two situations used to surface as the same bare "500 Internal Server Error":

  * macOS blocking the app from a protected folder (Desktop, Documents,
    Downloads) -- every freshly built, ad-hoc-signed app needs the grant
    again, and nothing in the UI said so.
  * the library file or music folder living on an external drive that
    isn't connected.

library_problems() probes both directly; describe_exception() maps an
exception that escaped a request onto the same vocabulary, so the error
the UI shows says what's wrong and what to do about it.
"""
import errno
import os
import sqlite3
import subprocess
import sys

PRIVACY_PANE_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders"


def _problem(kind, message, fix=None, path=None):
    return {"kind": kind, "message": message, "fix": fix, "path": path}


def _permission_problem(path):
    return _problem(
        "macos_permission",
        f"macOS is blocking Notorious B.P.M. from reading {path}. Open System Settings → "
        "Privacy & Security → Files & Folders (or Full Disk Access), switch it on for "
        "Notorious BPM, then press Retry.",
        fix="open_privacy_settings", path=path,
    )


def library_problems(db_path, music_dir):
    """Cheap direct probes of the two things everything else depends on."""
    problems = []
    if db_path:
        try:
            with open(db_path, "rb") as f:
                f.read(16)
        except FileNotFoundError:
            problems.append(_problem(
                "library_missing",
                f"The library file “{os.path.basename(db_path)}” can't be found. If it lives on an "
                "external drive, reconnect the drive and press Retry.",
                fix="retry", path=db_path,
            ))
        except PermissionError:
            problems.append(_permission_problem(db_path))
        except OSError as e:
            problems.append(_problem("library_unreadable", f"The library file can't be read ({e.strerror or e}).", fix="retry", path=db_path))
    music = music_problem(music_dir)
    if music:
        problems.append(music)
    return problems


def _volume_root(path):
    """/Volumes/<name> for a path on an external/network volume, else None."""
    parts = os.path.normpath(path).split(os.sep)
    if len(parts) >= 3 and parts[0] == "" and parts[1] == "Volumes":
        return os.sep + os.path.join("Volumes", parts[2])
    return None


def music_problem(music_dir):
    """None if the music folder is readable right now, else a problem dict.
    Reads one directory entry rather than listing the whole folder -- this
    runs on a timer, and listing a big folder on a sleeping external drive
    would spin it up for nothing."""
    if not music_dir:
        return None
    root = _volume_root(music_dir)
    if root and not os.path.ismount(root):
        return _problem(
            "music_missing",
            f"The drive “{os.path.basename(root)}” isn't connected. Reconnect it — nothing is lost, "
            "the library picks up where it left off.",
            fix="retry", path=music_dir,
        )
    try:
        with os.scandir(music_dir) as entries:
            next(entries, None)
    except FileNotFoundError:
        return _problem(
            "music_missing",
            f"Your music folder {music_dir} isn't reachable. If it's on an external drive, reconnect "
            "it — nothing is lost, the library picks up where it left off.",
            fix="retry", path=music_dir,
        )
    except PermissionError:
        return _permission_problem(music_dir)
    except OSError:
        return None
    return None


def music_dir_blocker(music_dir):
    """A sentence explaining why file-touching work shouldn't start right
    now, or None. Used before anything that writes tags or moves files: a
    bulk job started against an unplugged drive doesn't fail cleanly, it
    produces a pile of per-file errors (or, worse, decides the files are
    gone)."""
    if not music_dir:
        return "Set a music folder first (the Folder button), then try again."
    problem = music_problem(music_dir)
    return problem["message"] if problem else None


def describe_exception(exc, db_path, music_dir):
    """(kind, message) for an exception that escaped a request handler."""
    problems = library_problems(db_path, music_dir)
    if problems:
        return problems[0]["kind"], problems[0]["message"]
    if isinstance(exc, PermissionError) or (isinstance(exc, OSError) and exc.errno in (errno.EPERM, errno.EACCES)):
        return "macos_permission", _permission_problem(getattr(exc, "filename", None) or "a protected folder")["message"]
    if isinstance(exc, sqlite3.DatabaseError) and "malformed" in str(exc).lower():
        return "library_corrupt", (
            "The library file looks damaged. A snapshot is taken before every bulk change — "
            "restore the newest one from the library's backups folder."
        )
    return "internal", f"Something went wrong ({exc.__class__.__name__}). The details were saved to the log."


def open_privacy_settings():
    if sys.platform == "darwin":
        subprocess.run(["open", PRIVACY_PANE_URL])
        return True
    return False
