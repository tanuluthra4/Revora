"""Login throttling, response headers and the finding-status vocabulary."""
import re

import pytest

from services import login_throttle
from services.finding_service import STATUS_LABELS
from tests.test_routes import Browser


# ------------------------------------------------------------------- throttle
def test_throttle_unit_window_and_reset():
    t0 = 1000.0
    for i in range(login_throttle.MAX_FAILURES - 1):
        login_throttle.record_failure("1.2.3.4", "A@x.com", now=t0 + i)
    assert not login_throttle.is_blocked("1.2.3.4", "a@x.com", now=t0 + 20)          # case-insensitive key, not yet
    login_throttle.record_failure("1.2.3.4", "a@x.com", now=t0 + 21)
    assert login_throttle.is_blocked("1.2.3.4", "a@x.com", now=t0 + 22)
    assert not login_throttle.is_blocked("9.9.9.9", "a@x.com", now=t0 + 22)          # another client
    assert not login_throttle.is_blocked("1.2.3.4", "b@x.com", now=t0 + 22)          # another account
    assert not login_throttle.is_blocked("1.2.3.4", "a@x.com", now=t0 + login_throttle.WINDOW_SECONDS + 30)
    login_throttle.record_success("1.2.3.4", "a@x.com")
    assert not login_throttle.is_blocked("1.2.3.4", "a@x.com", now=t0 + 22)


def test_throttle_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(login_throttle, "_MAX_KEYS", 50)
    for i in range(500):
        login_throttle.record_failure("c", f"user{i}@x.com", now=1.0)
    assert len(login_throttle._failures) <= 50


def test_repeated_wrong_passwords_are_refused_then_correct_password_is_too(app):
    Browser(app).signup("victim", "victim@example.com")
    assert Browser(app).post("/login?role=developer", {"email": "victim@example.com",
                                                      "password": "correct-horse"}).status_code == 302   # control
    login_throttle.reset()
    b = Browser(app)
    for _ in range(login_throttle.MAX_FAILURES):
        r = b.post("/login?role=developer", {"email": "victim@example.com", "password": "wrong-password"})
        assert r.status_code == 200 and "Invalid email or password" in r.get_data(as_text=True)
    r = b.post("/login?role=developer", {"email": "victim@example.com", "password": "wrong-password"})
    assert r.status_code == 429
    r = b.post("/login?role=developer", {"email": "victim@example.com", "password": "correct-horse"})
    assert r.status_code == 429 and "Too many failed" in r.get_data(as_text=True)      # even the right one waits
    assert b.get("/dashboard").status_code == 302                                        # and no session was created


def test_successful_login_clears_the_count_and_other_accounts_are_unaffected(app):
    Browser(app).signup("one", "one@example.com"); Browser(app).signup("two", "two@example.com")
    b = Browser(app)
    for _ in range(login_throttle.MAX_FAILURES - 1):
        b.post("/login?role=developer", {"email": "one@example.com", "password": "nope-nope"})
    assert b.post("/login?role=developer", {"email": "one@example.com", "password": "correct-horse"}).status_code == 302
    for _ in range(login_throttle.MAX_FAILURES - 1):
        b.post("/login?role=developer", {"email": "one@example.com", "password": "nope-nope"})
    assert b.post("/login?role=developer", {"email": "one@example.com", "password": "correct-horse"}).status_code == 302
    for _ in range(login_throttle.MAX_FAILURES + 1):
        Browser(app).post("/login?role=developer", {"email": "ghost@example.com", "password": "x" * 9})
    assert Browser(app).post("/login?role=developer", {"email": "two@example.com", "password": "correct-horse"}).status_code == 302


# -------------------------------------------------------------------- headers
def test_security_headers_and_a_csp_that_forbids_inline_script(app):
    r = Browser(app).get("/")
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "'unsafe-inline'" not in csp.split("style-src")[0]
    assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp and "form-action 'self'" in csp
    assert r.headers["X-Content-Type-Options"] == "nosniff" and r.headers["X-Frame-Options"] == "DENY"


@pytest.mark.parametrize("path", ["/", "/login", "/signup"])
def test_no_page_contains_inline_script_or_event_handlers(app, path):
    html = Browser(app).get(path).get_data(as_text=True)
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)
    assert not re.search(r"\son(click|load|submit|error|focus)\s*=", html)


def test_templates_contain_no_inline_handlers():
    import os
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")
    for dp, _, files in os.walk(root):
        for f in files:
            text = open(os.path.join(dp, f), encoding="utf-8").read()
            assert not re.search(r"\son(click|load|submit|error|focus|change)\s*=", text), f
            assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", text), f


# ------------------------------------------------------------ finding labels
def test_finding_status_labels_match_the_specified_lifecycle():
    assert STATUS_LABELS == {
        "open": "Open",
        "acknowledged": "In progress",
        "fixed": "Fixed — awaiting verification",
        "confirmed": "Resolved — verified by the reviewer",
        "wont_fix": "Accepted — unresolved",
        "reopened": "Reopened — still unresolved",
    }
