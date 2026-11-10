"""Small in-memory brake on password guessing.

Failed sign-ins are counted per (client address, email) over a sliding window; once
the limit is reached further attempts are refused until the window passes. A
successful sign-in clears the count.

Honest limits (also in the README): the counters live in this process, so they
reset on restart and are not shared between workers; behind a proxy the client
address may be the proxy's, in which case the limit is effectively per email, and
anyone can then slow down sign-ins for an address they know. It reduces casual
guessing; it is not a substitute for strong passwords.
"""
import threading
import time

MAX_FAILURES = 10
WINDOW_SECONDS = 15 * 60
_MAX_KEYS = 10_000

_lock = threading.Lock()
_failures = {}          # key -> list[float] of failure times


def _key(address, email):
    return (address or "?", (email or "").strip().lower())


def _recent(key, now):
    times = [t for t in _failures.get(key, ()) if now - t < WINDOW_SECONDS]
    if times:
        _failures[key] = times
    else:
        _failures.pop(key, None)
    return times


def is_blocked(address, email, now=None):
    now = time.monotonic() if now is None else now
    with _lock:
        return len(_recent(_key(address, email), now)) >= MAX_FAILURES


def record_failure(address, email, now=None):
    now = time.monotonic() if now is None else now
    with _lock:
        if len(_failures) >= _MAX_KEYS:          # bounded memory under a flood of distinct keys
            for k in [k for k in _failures if not _recent(k, now)]:
                _failures.pop(k, None)
            if len(_failures) >= _MAX_KEYS:
                _failures.pop(next(iter(_failures)))
        key = _key(address, email)
        _failures.setdefault(key, []).append(now)


def record_success(address, email):
    with _lock:
        _failures.pop(_key(address, email), None)


def reset():
    with _lock:
        _failures.clear()
