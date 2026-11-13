"""HTTP-level tests: auth, CSRF, isolation, the end-to-end workflow, error paths."""
import re

import pytest

from tests.helpers import execute, query


class Browser:
    """A test client that automatically attaches the session's CSRF token."""

    def __init__(self, app):
        self.app = app
        self.client = app.test_client()

    def token(self):
        with self.client.session_transaction() as session:
            token = session.get("_csrf")
        if not token:
            self.client.get("/login")
            with self.client.session_transaction() as session:
                token = session["_csrf"]
        return token

    def get(self, url, **kwargs):
        return self.client.get(url, **kwargs)

    def post(self, url, data=None, csrf=True, **kwargs):
        data = dict(data or {})
        if csrf:
            data["csrf_token"] = self.token()
        return self.client.post(url, data=data, **kwargs)

    def signup(self, username, email=None, password="correct-horse", role="developer"):
        return self.post("/signup", {"username": username, "email": email or f"{username}@example.com",
                                     "password": password, "role": role})

    def login(self, email, password="correct-horse"):
        return self.post("/login", {"email": email, "password": password})


def list_profile(app, username, skills=("python",), expertise=(), visibility="listed", level="mid"):
    """Give a reviewer a listed profile directly in the test database."""
    uid = query(app, "SELECT id FROM users WHERE username = ?", (username,))[0][0]
    execute(app, "UPDATE profiles SET profile_visibility = ?, experience_level = ? WHERE user_id = ?",
            (visibility, level, uid))
    for kind, tags in (("skill", skills), ("expertise", expertise)):
        for t in tags:
            execute(app, "INSERT OR IGNORE INTO reviewer_tags (user_id, kind, tag, label) VALUES (?, ?, ?, ?)",
                    (uid, kind, t.lower(), t))
    return uid


def reviewer_ids(app, names):
    out = []
    for n in names:
        rows = query(app, "SELECT id FROM users WHERE username = ? OR email = ?", (n, n))
        out.append(str(rows[0][0]) if rows else "999999")
    return out


def choose(browser, app, task_id, names, required=1, **kw):
    return browser.post(f"/tasks/{task_id}/reviewer",
                        {"reviewer_ids": reviewer_ids(app, names), "required": str(required)}, **kw)


@pytest.fixture
def alice(app):
    b = Browser(app)
    assert b.signup("alice").status_code == 302
    return b


@pytest.fixture
def bob(app):
    b = Browser(app)
    assert b.signup("bob").status_code == 302
    return b


@pytest.fixture
def reviewer(app):
    b = Browser(app)
    assert b.signup("rev", "rev@example.com", role="reviewer").status_code == 302
    list_profile(app, "rev")
    return b


def create_task(browser, title="Ship a REST API", **extra):
    """Create a review request (project + version 1) through the real form. Returns its task id."""
    from tests.review_helpers import VERSION_FORM
    browser.post("/projects/new", {"title": f"Project for {title}", "status": "Active"})
    page = browser.get("/reviews/new").get_data(as_text=True)
    project_id = re.findall(r'<option value="(\d+)"', page)[-1]
    response = browser.post("/reviews/new", dict(VERSION_FORM, project_id=project_id, title=title, **extra))
    assert response.status_code == 302, response.get_data(as_text=True)
    return int(re.search(r"/reviews/(\d+)", response.headers["Location"]).group(1))


def fire(browser, task_id, event, reason=None):
    data = {"reason": reason} if reason else {}
    return browser.post(f"/tasks/{task_id}/events/{event}", data, follow_redirects=True)


def state(app, task_id):
    return query(app, "SELECT current_state FROM workflow_instances WHERE task_id = ?", (task_id,))[0][0]


# ------------------------------------------------------------------- auth
def test_protected_pages_redirect_when_logged_out(app):
    b = Browser(app)
    for url in ("/dashboard", "/reviews/new", "/projects", "/projects/new", "/tasks/1"):
        response = b.get(url)
        assert response.status_code == 302 and "/login" in response.headers["Location"], url
    assert b.get("/review").status_code == 302


def test_signup_validation(app):
    b = Browser(app)
    cases = [
        ({"username": "ab", "email": "a@b.co", "password": "longenough"}, "Username"),
        ({"username": "valid_name", "email": "not-an-email", "password": "longenough"}, "valid email"),
        ({"username": "valid_name", "email": "a@b.co", "password": "short"}, "Password"),
    ]
    for data, fragment in cases:
        response = b.post("/signup", data)
        assert response.status_code == 200 and fragment in response.get_data(as_text=True)
    assert query(app, "SELECT COUNT(*) FROM users")[0][0] == 0


def test_signup_duplicates_and_password_hashing(app, alice):
    other = Browser(app)
    response = other.post("/signup", {"username": "alice2", "email": "ALICE@example.com", "password": "correct-horse"})
    assert "Email already exists" in response.get_data(as_text=True)
    response = other.post("/signup", {"username": "alice", "email": "new@example.com", "password": "correct-horse"})
    assert "Username already taken" in response.get_data(as_text=True)
    stored = query(app, "SELECT password FROM users WHERE username = 'alice'")[0][0]
    assert stored != "correct-horse" and stored.startswith(("scrypt:", "pbkdf2:"))


def test_home_page_offers_separate_role_portals(app):
    b = Browser(app)
    response = b.get("/")
    html = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "I want a review" in html      # landing copy rewritten for Revora
    assert "I want to review" in html
    assert "/login?role=developer" in html
    assert "/login?role=reviewer" in html


def test_login_portal_checks_actual_account_role(app, alice, reviewer):
    # A developer cannot gain reviewer permissions by choosing the reviewer portal.
    developer_attempt = alice.post("/login?role=reviewer", {
        "email": "alice@example.com", "password": "correct-horse", "role": "reviewer"
    }, follow_redirects=True)
    assert "developer account" in developer_attempt.get_data(as_text=True)
    assert reviewer.get("/review").status_code == 200


def test_reviewer_login_redirects_to_review_queue(app, reviewer):
    # The fixture creates a configured reviewer and leaves that account logged in;
    # use a fresh browser to exercise the actual reviewer login form.
    b = Browser(app)
    response = b.post("/login?role=reviewer", {
        "email": "rev@example.com", "password": "correct-horse", "role": "reviewer"
    })
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/review")


def test_login_logout_flow(app, alice):
    b = Browser(app)
    assert b.login("alice@example.com", "wrong-password").status_code == 200
    assert b.get("/dashboard").status_code == 302
    assert b.login("Alice@Example.com").status_code == 302          # e-mail is case-insensitive
    assert b.get("/dashboard").status_code == 200
    assert b.get("/logout").status_code == 405                      # logout is POST-only
    assert b.post("/logout").status_code == 302
    assert b.get("/dashboard").status_code == 302


def test_csrf_token_is_required_for_every_post(app, alice):
    task_id = create_task(alice)
    for url in ("/login", "/signup", "/logout", "/reviews/new", f"/tasks/{task_id}/delete",
                f"/tasks/{task_id}/events/START", "/projects/new", f"/reviews/{task_id}/report"):
        response = alice.post(url, {"title": "x"}, csrf=False)
        assert response.status_code == 400, url
    assert state(app, task_id) == "CREATED"
    assert alice.post(f"/tasks/{task_id}/delete", {"csrf_token": "forged"}, csrf=False).status_code == 400


def test_session_cookie_flags(app, alice):
    cookie = alice.client.get_cookie("session")
    assert cookie.http_only is True and cookie.same_site == "Lax"


def test_secret_key_is_required_in_production(monkeypatch):
    from app import create_app
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app({"ENV": "production", "SECRET_KEY": None, "DATABASE": ":memory:"})


# --------------------------------------------------------------- tasks CRUD
def test_project_urls_must_be_http(app, alice):
    response = alice.post("/projects/new", {"title": "P", "status": "Active", "github_url": "javascript:alert(1)"})
    assert response.status_code == 200
    assert query(app, "SELECT COUNT(*) FROM projects")[0][0] == 0
    ok = alice.post("/projects/new", {"title": "P", "status": "Active", "github_url": "https://github.com/o/r"})
    assert ok.status_code == 302


def test_dashboard_with_no_tasks_has_an_empty_state(app, alice):
    assert "Nothing here yet" in alice.get("/dashboard").get_data(as_text=True)


# ------------------------------------------------------------- misc / errors
def test_error_pages_do_not_leak_internals(app, alice):
    page = alice.get("/tasks/999999")
    text = page.get_data(as_text=True)
    assert page.status_code == 404 and "Traceback" not in text
    assert alice.get("/nope").status_code == 404


def test_healthz_and_security_headers(app):
    response = Browser(app).get("/healthz")
    assert response.get_json() == {"status": "ok"}
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"


def test_signup_creates_the_chosen_role_and_lands_on_the_right_home(app):
    dev, rev = Browser(app), Browser(app)
    assert dev.signup("devon", role="developer").headers["Location"].endswith("/dashboard")
    assert rev.signup("riley", role="reviewer").headers["Location"].endswith("/review")
    assert query(app, "SELECT role FROM users WHERE username = 'devon'")[0][0] == "developer"
    assert query(app, "SELECT role FROM users WHERE username = 'riley'")[0][0] == "reviewer"
    assert b"reviewer" in rev.get("/signup?role=reviewer").data
    assert Browser(app).get("/signup?role=admin").status_code == 400


def test_reviewer_signup_is_open_without_invite_or_allowlist(app):
    html = Browser(app).get("/signup?role=reviewer").get_data(as_text=True)
    assert "invite" not in html.lower() and 'name="invite_code"' not in html
    for name in ("openrev", "another"):
        assert Browser(app).signup(name, role="reviewer").status_code == 302
    assert [r[0] for r in query(app, "SELECT role FROM users ORDER BY id")] == ["reviewer", "reviewer"]
    # the old configuration keys no longer exist
    for key in ("REVIEWER_EMAILS", "REVIEWER_INVITE_CODE", "REVIEWER_OPEN_SIGNUP"):
        assert key not in app.config


def test_signup_cannot_forge_other_roles(app):
    b = Browser(app)
    assert b.post("/signup", {"username": "mallory", "email": "m@example.com", "password": "correct-horse",
                              "role": "admin"}).status_code == 400
    assert query(app, "SELECT COUNT(*) FROM users")[0][0] == 0
    # extra form fields cannot smuggle a role either
    b.post("/signup?role=developer", {"username": "mallory", "email": "m@example.com", "password": "correct-horse",
                                      "is_admin": "1", "user_role": "reviewer"})
    assert query(app, "SELECT role FROM users")[0][0] == "developer"


def submit_for(browser, task_id, reviewer_name, required=1):
    fire(browser, task_id, "START")
    fire(browser, task_id, "REQUEST_EVIDENCE")
    browser.post(f"/tasks/{task_id}/evidence", {"evidence_type": "github_repo",
                                                "evidence_url": "https://github.com/octocat/hello"})
    choose(browser, browser.app, task_id, reviewer_name.split("\n"), required)
    fire(browser, task_id, "SUBMIT_EVIDENCE")


def verify_task(app, alice, reviewer, title="Shareable work"):
    task_id = create_task(alice, title)
    submit_for(alice, task_id, "rev")
    fire(reviewer, task_id, "START_VALIDATION")
    fire(reviewer, task_id, "VOTE_APPROVE", "Matches the claim.")
    assert state(app, task_id) == "VERIFIED"
    return task_id


@pytest.fixture
def trio(app):
    names = {}
    for n in ("rev_a", "rev_b", "rev_c"):
        b = Browser(app)
        assert b.signup(n, role="reviewer").status_code == 302
        list_profile(app, n)
        names[n] = b
    return names


def to_validating(app, alice, task_id, trio, required=2):
    submit_for(alice, task_id, "rev_a\nrev_b\nrev_c", required)
    assert state(app, task_id) == "EVIDENCE_SUBMITTED"
    fire(trio["rev_a"], task_id, "START_VALIDATION")
    assert state(app, task_id) == "VALIDATING"


def test_landing_page_seo_and_navigation(app):
    html = Browser(app).get("/").get_data(as_text=True)
    assert 'name="description" content="Revora is a project review and improvement platform' in html
    assert 'property="og:title"' in html and 'rel="icon"' in html
    assert "noindex" not in html
    for anchor in ("#how-it-works", "#goals", "#trust", "#faq"):
        assert f'href="{anchor}"' in html and f'id="{anchor[1:]}"' in html
    assert 'href="#"' not in html                       # no dead links
    assert "canonical" not in html                      # PUBLIC_BASE_URL not configured
    app.config["PUBLIC_BASE_URL"] = "https://example.test"
    assert 'rel="canonical" href="https://example.test/"' in Browser(app).get("/").get_data(as_text=True)


def test_private_pages_are_noindex_and_robots_is_served(app, alice):
    assert "noindex" in alice.get("/dashboard", follow_redirects=True).headers.get("X-Robots-Tag", "")
    assert "noindex" in Browser(app).get("/login").headers.get("X-Robots-Tag", "")
    assert 'name="robots" content="noindex' in Browser(app).get("/login").get_data(as_text=True)
    robots = Browser(app).get("/robots.txt")
    assert robots.status_code == 200 and "Disallow: /" in robots.get_data(as_text=True)
    assert Browser(app).get("/favicon.ico").status_code == 200
