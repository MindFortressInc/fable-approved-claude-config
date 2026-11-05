#!/usr/bin/env python3
"""Load a shared review lens list into deep-review (optional, off by default).

A lens file is markdown with a `## Generic Review Invariants` section and a
numbered `## Commonly Missed` section: the defect classes external reviewers
most often catch after deep-review has passed a PR. Keeping it in one place
lets several harnesses (or teammates) read the same list instead of
hand-copying lenses into each review prompt.

Configure it with COMMONLY_MISSED_SOURCE:

  unset or empty     off. Both commands print nothing and exit 0.
  /path/to/file.md   read the file in place (`~` is expanded).
  an https:// URL    fetch it, then cache it at
                     ~/.claude/review/commonly-missed.md so a later review
                     still has it when the fetch fails.

  commonly-missed.py sync     load (and for a URL, cache) the lens file;
                              exit 0 if it loaded fresh or the loader is off,
                              1 otherwise
  commonly-missed.py prompt   load it, then print the block deep-review pastes
                              into every Step 3 agent prompt; always exit 0

Content missing either required section is rejected, and a fetched copy that
fails the check is never cached. A fetch must stay on https (redirects
included), carry no credentials in the URL, and finish within TIMEOUT seconds.
If the source fails, a cached copy from the same URL is used with an INFO line
naming its age; a copy cached from a different URL is never used. With
nothing usable, `prompt` prints
nothing on stdout and one INFO line on stderr, and the review continues with
its built-in lenses. A missing lens file never blocks a review.
"""
import datetime
import hashlib
import http.client
import os
import re
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

ENV = "COMMONLY_MISSED_SOURCE"
INVARIANTS = "## Generic Review Invariants"
CLASSES = "## Commonly Missed"
TIMEOUT = 30          # whole fetch, not per socket read
MAX_BYTES = 2 * 1024 * 1024

PREAMBLE = """\
> "Check the diff against each Generic Review Invariant and each numbered
> Commonly Missed class below explicitly. For each one that applies, either
> report the finding (with the required evidence format) or state in one line
> why the diff is clean of it. These are the classes external reviewers most
> often caught after this process had passed a PR."
"""


def _info(msg):
    print("INFO: commonly-missed: " + msg, file=sys.stderr)


def _source():
    return (os.environ.get(ENV) or "").strip()


def _is_url(src):
    return re.match(r"https?://", src, re.I) is not None


def _cache():
    return os.path.join(os.path.expanduser("~"), ".claude", "review", "commonly-missed.md")


def _cache_source():
    """Sidecar holding a hash of the full URL the cached copy came from.

    The full URL, query included, keys the cache (two sources that differ only
    by `?ref=` must not share it), but it is hashed so a token in the query
    string never lands on disk.
    """
    return _cache() + ".source"


def _source_key(url):
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _display(path):
    """`path` with the home directory shown as `~`: INFO lines may be published."""
    home = os.path.expanduser("~")
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def _missing(text):
    """The required headings `text` lacks; [] means it is a usable lens file."""
    present = {line.rstrip() for line in (text or "").splitlines()}
    return [h for h in (INVARIANTS, CLASSES) if h not in present]


def _missing_msg(text):
    return "content has no %s section" % " or ".join(repr(h) for h in _missing(text))


def _read_file(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read(), None
    except (OSError, UnicodeDecodeError) as exc:
        return None, "cannot read %s (%s)" % (_display(path), getattr(exc, "strerror", None) or type(exc).__name__)


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only while it stays on https."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith("https://"):
            raise urllib.error.HTTPError(req.full_url, code, "redirect leaves https", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url):
    return urllib.request.build_opener(_HttpsOnlyRedirects).open(url, timeout=TIMEOUT)


def _download(url):
    """(body, None) or (None, reason): the network half of `_fetch`."""
    try:
        with _open(url) as resp:
            if not resp.geturl().lower().startswith("https://"):
                return None, "the fetch left https"
            return resp.read(MAX_BYTES + 1), None
    except urllib.error.HTTPError as exc:
        return None, "fetch failed (HTTP %d)" % exc.code
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
        return None, "fetch failed (%s)" % type(exc).__name__


def _fetch(url):
    """(text, None) or (None, a short fixed reason).

    The reason never quotes the server's response body or the URL's query
    string: deep-review copies the INFO line into a review that may be
    published, and a URL can carry a token.
    """
    if not url.lower().startswith("https://"):
        return None, "only https:// URLs are fetched"
    try:
        parts = urllib.parse.urlsplit(url)
        has_userinfo = parts.username is not None or parts.password is not None
    except ValueError:
        return None, "the URL does not parse"
    if has_userinfo:
        return None, "a URL with credentials in it is refused"
    # The socket timeout bounds each read, not the fetch: a server dripping a
    # byte just under it keeps one read (or the headers) going indefinitely.
    # So the whole open-and-read runs on a daemon thread joined with TIMEOUT;
    # a fetch still running then is abandoned and dies with the process.
    box = []

    def run():
        try:
            box.append(_download(url))
        except Exception as exc:
            box.append(exc)

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(TIMEOUT)
    if not box:
        return None, "fetch timed out after %ds" % TIMEOUT
    if isinstance(box[0], Exception):
        raise box[0]
    body, err = box[0]
    if err:
        return None, err
    if len(body) > MAX_BYTES:
        return None, "fetched file is over %d bytes" % MAX_BYTES
    try:
        return body.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "fetched file is not UTF-8"


def _write(dest, text):
    tmp = dest + ".tmp.%d" % os.getpid()
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, dest)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return "cannot write %s (%s)" % (_display(dest), exc.strerror or type(exc).__name__)
    return None


def _origin(url):
    """The URL without its path's query string or fragment, for the Source line."""
    return re.split(r"[?#]", url, maxsplit=1)[0]


def sync():
    """Return (text or None, source description, loaded fresh?)."""
    src = _source()
    if not src:
        return None, None, True
    if not _is_url(src):
        path = os.path.expanduser(src)
        text, err = _read_file(path)
        if err is None and _missing(text):
            err = "%s: %s" % (_display(path), _missing_msg(text))
        if err:
            _info("lens file unavailable (%s); continuing with the built-in lenses only" % err)
            return None, None, False
        return text, _display(path), True
    origin = _origin(src)
    text, err = _fetch(src)
    if err is None and _missing(text):
        err = _missing_msg(text)
    if err is None:
        # Unclaim the cache, write it, then claim it: a failure part-way leaves
        # a copy no source claims, never one claimed by the wrong URL.
        try:
            os.unlink(_cache_source())
        except OSError:
            pass
        write_err = _write(_cache(), text) or _write(_cache_source(), _source_key(src) + "\n")
        if write_err:
            _info("fetched but not cached (%s); using the fetched copy" % write_err)
        return text, origin, True
    dest = _cache()
    cached_from, _ = _read_file(_cache_source()) if os.path.exists(_cache_source()) else (None, None)
    cached = None
    if (cached_from or "").strip() == _source_key(src) and os.path.exists(dest):
        cached, _ = _read_file(dest)
    if cached is not None and not _missing(cached):
        age = datetime.datetime.fromtimestamp(os.path.getmtime(dest)).isoformat(timespec="minutes")
        _info("not refreshed (%s); using the copy cached %s" % (err, age))
        return cached, "cached copy of %s from %s" % (origin, age), False
    _info("lens file unavailable (%s); continuing with the built-in lenses only" % err)
    return None, None, False


def _section(lines, heading):
    out, on = [], False
    for line in lines:
        if re.match(r"#{1,2} ", line):  # a `# ` heading ends a section too
            on = line.rstrip() == heading
        if on:
            out.append(line)
    return "".join(out).rstrip("\n")


def prompt():
    text, source, _ = sync()
    if text is None:
        return
    lines = text.splitlines(keepends=True)
    parts = [PREAMBLE.rstrip("\n"), "Source: `%s`." % source]
    for heading in (INVARIANTS, CLASSES):
        parts.append(_section(lines, heading))
    print("\n\n".join(parts))


def main(argv):
    if argv[1:] == ["sync"]:
        try:
            text, source, fresh = sync()
        except Exception as exc:
            _info("loader failed (%s)" % type(exc).__name__)
            return 1
        if text is not None:
            print("lens file <- %s" % source)
        return 0 if fresh else 1
    if argv[1:] == ["prompt"]:
        try:
            prompt()
        except Exception as exc:  # the lens list is advisory: never block a review
            _info("loader failed (%s); continuing with the built-in lenses only"
                  % type(exc).__name__)
        return 0
    print("usage: commonly-missed.py sync|prompt", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
