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
fails the check is never cached. If the source fails, an existing cached copy
is used with an INFO line naming its age. With nothing usable, `prompt` prints
nothing on stdout and one INFO line on stderr, and the review continues with
its built-in lenses. A missing lens file never blocks a review.
"""
import datetime
import os
import re
import sys
import urllib.error
import urllib.request

ENV = "COMMONLY_MISSED_SOURCE"
INVARIANTS = "## Generic Review Invariants"
CLASSES = "## Commonly Missed"
TIMEOUT = 30
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
        return None, "cannot read %s (%s)" % (path, getattr(exc, "strerror", None) or type(exc).__name__)


def _fetch(url):
    """(text, None) or (None, a short fixed reason).

    The reason never quotes the server's response body or the URL's query
    string: deep-review copies the INFO line into a review that may be
    published, and a URL can carry a token.
    """
    if not url.lower().startswith("https://"):
        return None, "only https:// URLs are fetched"
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
            body = resp.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        return None, "fetch failed (HTTP %d)" % exc.code
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, "fetch failed (%s)" % type(exc).__name__
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
        return "cannot write %s (%s)" % (dest, exc.strerror or type(exc).__name__)
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
            err = "%s: %s" % (path, _missing_msg(text))
        if err:
            _info("lens file unavailable (%s); continuing with the built-in lenses only" % err)
            return None, None, False
        return text, path, True
    text, err = _fetch(src)
    if err is None and _missing(text):
        err = _missing_msg(text)
    if err is None:
        write_err = _write(_cache(), text)
        if write_err:
            _info("fetched but not cached (%s); using the fetched copy" % write_err)
        return text, _origin(src), True
    dest = _cache()
    cached, _ = _read_file(dest) if os.path.exists(dest) else (None, None)
    if cached is not None and not _missing(cached):
        age = datetime.datetime.fromtimestamp(os.path.getmtime(dest)).isoformat(timespec="minutes")
        _info("not refreshed (%s); using the copy cached %s" % (err, age))
        return cached, "cached copy from %s" % age, False
    _info("lens file unavailable (%s); continuing with the built-in lenses only" % err)
    return None, None, False


def _section(lines, heading):
    out, on = [], False
    for line in lines:
        if line.startswith("## "):
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
        text, source, fresh = sync()
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
