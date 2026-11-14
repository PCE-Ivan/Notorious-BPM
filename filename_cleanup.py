"""Tidies leftover copy numbers out of file names.

A second copy of a song arrives as "Song (2).mp3" (a downloader, or this app
dodging a name collision), "Song 2.mp3" / "Song copy.mp3" (Finder) or
"Song 1.mp3" (iTunes). Remove the duplicates and the copy that was kept may
be the one still carrying the number. This puts the plain name back.

The hard part is not renaming songs whose real name ends in a number --
"Freedom 90", "Perfect 10", "Strawberry Letter 23", "21". So:

  "(2)"       a deliberate marker nobody types into a title by accident.
              Removed unless the track's own title tag ends with that number.
  " 2" / "_2" only a single digit, only when the file has a real title tag,
  "copy"      that title does not end with the number (or "copy"), and what
              is left of the name still ends with the title -- i.e. the
              number is provably extra.
  ".mp3.mp3"  a doubled extension is collapsed.

Nothing is ever overwritten: if the plain name is taken (the other copy is
still there) the file is reported as blocked instead. `plan()` only looks;
`apply()` re-derives the plan itself and renames just the ids it is handed.
"""
import os
import re
import sqlite3
import unicodedata

from fs_safety import safe_move

_PAREN_RE = re.compile(r"^(?P<base>.*\S)\s*\((?P<n>\d{1,2})\)$")
_COPY_RE = re.compile(r"^(?P<base>.*\S)[\s_-]+copy(?:[\s_-]*(?P<n>\d{1,2}))?$", re.IGNORECASE)
_BARE_RE = re.compile(r"^(?P<base>.*[^\d\s_-])[\s_-]+(?P<n>\d{1,2})$")
_BARE_MAX = 9


def _norm(text):
    """Letters and digits only, case-folded -- file names lose punctuation
    ("Freedom! '90" is saved as "Freedom 90"), so that's all that compares."""
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return "".join(ch for ch in text if ch.isalnum())


def _ends_with_number(norm_title, digits):
    return norm_title.endswith(digits) or norm_title.endswith(digits.lstrip("0") or "0")


def clean_name(filename, title=None, title_is_real=False):
    """(new_filename, reason) or (None, None) when the name is fine as it is."""
    stem, ext = os.path.splitext(filename)
    reasons = []

    while True:
        inner_stem, inner_ext = os.path.splitext(stem)
        if inner_stem and ext and inner_ext.lower() == ext.lower():
            stem = inner_stem
            if "doubled " + ext.lower() not in reasons:
                reasons.append("doubled " + ext.lower())
        else:
            break

    norm_title = _norm(title) if title_is_real else ""
    for _ in range(3):  # "Song (2) (1)" -- but never loop on something odd
        m = _PAREN_RE.match(stem)
        if m:
            if norm_title and _ends_with_number(norm_title, m.group("n")):
                break  # the number is part of the song's name
            stem = m.group("base")
            reasons.append("copy number (%s)" % m.group("n"))
            continue
        if not norm_title:
            break  # the rules below need a real title to prove the number is extra
        m = _COPY_RE.match(stem)
        if m:
            tail = "copy" + (m.group("n") or "")
            if norm_title.endswith(tail) or norm_title.endswith("copy") or not _norm(m.group("base")).endswith(norm_title):
                break
            stem = m.group("base")
            reasons.append("“copy”")
            continue
        m = _BARE_RE.match(stem)
        if m:
            n = m.group("n")
            if int(n) < 1 or int(n) > _BARE_MAX or _ends_with_number(norm_title, n):
                break
            if not _norm(m.group("base")).endswith(norm_title):
                break
            stem = m.group("base")
            reasons.append("copy number %s" % n)
            continue
        break

    stem = stem.rstrip(" ._-")
    new_name = stem + ext
    if not stem or not reasons or new_name == filename:
        return None, None
    return new_name, ", ".join(reasons)


def _title_is_real(row, stem):
    """Is `title` an actual tag, or just the file name echoed back by the
    scan? has_title_tag knows when the deep scan has run; otherwise a title
    identical to the file name is assumed to be the echo."""
    flag = row["has_title_tag"] if "has_title_tag" in row.keys() else None
    if flag is not None:
        return bool(flag)
    return bool(row["title"]) and _norm(row["title"]) != _norm(stem)


def plan(rows, music_dir):
    """rows: sqlite3.Row (or dict-like with .keys()) of id, path, title,
    artist, has_title_tag. Returns {"items": [...], "blocked": [...]} --
    items can be renamed, blocked ones can't yet (and why)."""
    known_paths = set()
    candidates = []
    for row in rows:
        rel = row["path"] or ""
        known_paths.add(rel)
        directory, filename = os.path.split(rel)
        stem = os.path.splitext(filename)[0]
        new_name, reason = clean_name(filename, row["title"], _title_is_real(row, stem))
        if new_name:
            candidates.append({
                "id": row["id"], "artist": row["artist"] or "", "title": row["title"] or "",
                "folder": directory, "old_name": filename, "new_name": new_name, "reason": reason,
                "old_path": rel, "new_path": os.path.join(directory, new_name),
            })

    # Lowest-numbered copy gets the plain name when several want the same one.
    candidates.sort(key=lambda c: (c["new_path"].lower(), len(c["old_name"]), c["old_name"].lower()))
    items, blocked, claimed = [], [], set()
    for c in candidates:
        if not os.path.isfile(os.path.join(music_dir, c["old_path"])):
            continue  # not on disk right now (unplugged drive, already moved): nothing to offer
        key = c["new_path"].lower()
        if c["new_path"] in known_paths or os.path.lexists(os.path.join(music_dir, c["new_path"])):
            c["why"] = "A file with the plain name is still there — these two are probably duplicates."
            blocked.append(c)
        elif key in claimed:
            c["why"] = "Another copy is getting the plain name."
            blocked.append(c)
        else:
            claimed.add(key)
            items.append(c)
    order = lambda c: (c["artist"].lower(), c["old_name"].lower())  # noqa: E731
    return {"items": sorted(items, key=order), "blocked": sorted(blocked, key=order)}


def apply(db_path, music_dir, ids):
    """Renames the planned files whose track id is in `ids`. The plan is
    rebuilt here from the library as it is now -- names from the browser are
    never trusted. Returns {"renamed", "errors", "path_moves"}; path_moves
    ({old: new}, relative) is what the caller journals for Undo."""
    wanted = {int(i) for i in ids}
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    path_moves, errors = {}, []
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tracks)")}
        flag = "has_title_tag" if "has_title_tag" in cols else "NULL AS has_title_tag"
        rows = conn.execute("SELECT id, path, title, artist, %s FROM tracks" % flag).fetchall()
        for item in plan(rows, music_dir)["items"]:
            if item["id"] not in wanted:
                continue
            src = os.path.join(music_dir, item["old_path"])
            dst = os.path.join(music_dir, item["new_path"])
            try:
                if os.path.lexists(dst):
                    raise OSError("a file with that name appeared in the meantime")
                safe_move(src, dst)
            except OSError as e:
                errors.append({"track_id": item["id"], "name": item["old_name"], "error": str(e)})
                continue
            # One commit per file: a crash mid-run must never leave a renamed
            # file the library still looks for under its old name.
            conn.execute("UPDATE tracks SET path=? WHERE id=?", (item["new_path"], item["id"]))
            conn.commit()
            path_moves[item["old_path"]] = item["new_path"]
    finally:
        conn.close()
    return {"renamed": len(path_moves), "errors": errors, "path_moves": path_moves}
