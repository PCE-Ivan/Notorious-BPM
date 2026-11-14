"""The app's version, in one place: About, the update check, and the build
(jukebox_macos.spec reads it for the bundle's Info.plist)."""
VERSION = "1.3"
RELEASES_API = "https://api.github.com/repos/PCE-Ivan/Notorious-BPM/releases/latest"
RELEASES_PAGE = "https://github.com/PCE-Ivan/Notorious-BPM/releases/latest"


def parse(text):
    """'v1.3' / '1.3.1' -> (1, 3) / (1, 3, 1). Anything unparseable sorts lowest."""
    parts = []
    for piece in (text or "").strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def is_newer(candidate, current=VERSION):
    a, b = parse(candidate), parse(current)
    n = max(len(a), len(b))
    return bool(a) and a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))
