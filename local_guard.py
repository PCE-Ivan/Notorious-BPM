"""Keeps other websites from talking to this app's local server.

The app is a Flask server on 127.0.0.1 with a fixed-ish port. Anything running
in the user's browser can *send* requests there: a page on any site can POST
text/plain to http://127.0.0.1:<port>/api/... without a CORS preflight, and
`get_json(force=True)` is happy to parse it -- which would let a stranger's web
page empty the Trash, move files around, or switch the library. And with DNS
rebinding a page can read responses too.

The window and any browser tab the user opens themselves always talk to
127.0.0.1 / localhost and send same-origin requests, so the rule is simple:

  * the Host header must be this machine (stops DNS rebinding);
  * an Origin header, when present, must be this very server;
  * Sec-Fetch-Site (sent by every current browser) must say same-origin or
    none (typed into the address bar). The one exception is a plain page
    navigation (GET), which can't read anything back.

Requests with no browser headers at all (curl, the test client, the
launcher's own probes) are allowed -- a web page can't send those.

Also here: `refuse_private_url`, for the radio proxy, which fetches a URL on
the page's behalf and shouldn't be a way to poke at the user's router.
"""
import ipaddress
import socket
import urllib.parse

LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "[::1]", "::1"}


def _hostname(host_header):
    """'127.0.0.1:5151' -> '127.0.0.1';  '[::1]:5151' -> '[::1]'."""
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[: end + 1] if end != -1 else host
    return host.rsplit(":", 1)[0] if ":" in host else host


def check_request(method, host, origin=None, sec_fetch_site=None, sec_fetch_mode=None):
    """None if the request may proceed, else a short reason it was refused."""
    if _hostname(host) not in LOCAL_HOSTNAMES:
        return "unexpected Host header"
    if origin is not None and origin.strip():
        parsed = urllib.parse.urlparse(origin.strip())
        if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != (host or "").strip().lower():
            return "cross-origin request"
    site = (sec_fetch_site or "").strip().lower()
    if site and site not in ("same-origin", "none"):
        is_navigation = (method or "").upper() in ("GET", "HEAD") and (sec_fetch_mode or "").lower() == "navigate"
        if not is_navigation:
            return "cross-site request"
    return None


def refuse_private_url(url):
    """None if `url` is a public http(s) address, else why not. Resolves the
    name, so a hostname that points at the LAN or loopback is refused too."""
    parsed = urllib.parse.urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "not an http(s) URL"
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80),
                                   type=socket.SOCK_STREAM)
    except OSError:
        return None  # unresolvable: let the real request fail with its own error
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return "address is on this machine or the local network"
    return None
