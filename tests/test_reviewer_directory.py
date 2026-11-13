"""Open reviewer registration, profiles, directory, privacy and the reviewer picker."""
import re

import pytest

from services import reviewer_profiles as rp
from tests.helpers import execute, query
from tests.test_routes import (Browser, choose, create_task, fire, list_profile, reviewer_ids, state,
                               submit_for)


def reviewer_browser(app, name, **profile):
    b = Browser(app)
    assert b.signup(name, role="reviewer").status_code == 302
    if profile:
        list_profile(app, name, **profile)
    return b


def profile_form(**over):
    data = {"display_name": "Rita Reviewer", "headline": "Backend engineer", "bio": "I review APIs.",
            "skills": "Python, Flask, SQL", "expertise": "API design", "experience_level": "senior",
            "work_history": "Built a payments API.", "education": "B.Tech", "github_url": "https://github.com/rita",
            "portfolio_url": "https://rita.example.com", "linkedin_url": "", "profile_visibility": "listed",
            "pub_bio": "1", "pub_experience": "1", "pub_work": "1", "pub_education": "1", "pub_links": "1"}
    data.update(over)
    return data


# ---------------------------------------------------------------- profiles
def test_reviewer_creates_and_edits_profile(app):
    rita = reviewer_browser(app, "rita")
    page = rita.get("/reviewer/profile").get_data(as_text=True)
    assert "Profile completeness" in page and "self-reported" in page
    assert rita.post("/reviewer/profile", profile_form()).status_code == 302
    row = query(app, "SELECT full_name, experience_level, profile_visibility FROM profiles")[0]
    assert tuple(row) == ("Rita Reviewer", "senior", "listed")
    assert sorted(r[0] for r in query(app, "SELECT label FROM reviewer_tags WHERE kind = 'skill'")) == ["Flask", "Python", "SQL"]
    rita.post("/reviewer/profile", profile_form(skills="Rust", display_name="Rita R."))
    assert [r[0] for r in query(app, "SELECT label FROM reviewer_tags WHERE kind = 'skill'")] == ["Rust"]
    assert query(app, "SELECT COUNT(*) FROM profiles")[0][0] == 1          # edited, not duplicated


def test_profile_validation_rejects_bad_input(app):
    rita = reviewer_browser(app, "rita")
    bad = [{"github_url": "javascript:alert(1)"}, {"portfolio_url": "ftp://x.example.com"},
           {"github_url": "https://evil.example.com/rita"}, {"display_name": "x"},
           {"bio": "b" * 601}, {"experience_level": "god"}, {"profile_visibility": "everyone"},
           {"skills": "<script>"}, {"skills": "", "expertise": ""}, {"skills": ",".join(f"s{i}" for i in range(16))}]
    for override in bad:
        r = rita.post("/reviewer/profile", profile_form(**override))
        assert r.status_code == 200, override
    assert query(app, "SELECT profile_visibility FROM profiles")[0][0] == "private"


def test_profile_post_cannot_change_role_or_account_fields(app):
    rita = reviewer_browser(app, "rita")
    rita.post("/reviewer/profile", profile_form(role="developer", is_admin="1", email="x@evil.example",
                                                user_id="999", username="hacked"))
    row = query(app, "SELECT username, email, role FROM users")[0]
    assert tuple(row) == ("rita", "rita@example.com", "reviewer")


def test_only_reviewers_can_edit_profiles(app):
    dev = Browser(app)
    dev.signup("devon")
    assert dev.get("/reviewer/profile").status_code == 403      # reviewer-only area
    assert dev.post("/reviewer/profile", profile_form()).status_code == 403
    assert query(app, "SELECT COUNT(*) FROM reviewer_tags")[0][0] == 0
    anon = Browser(app)
    assert anon.get("/reviewer/profile").status_code == 302 and "/login" in anon.get("/reviewer/profile").headers["Location"]


def test_listing_requires_a_skill_and_defaults_to_private(app):
    rita = reviewer_browser(app, "rita")
    assert query(app, "SELECT profile_visibility FROM profiles")[0][0] == "private"
    assert Browser(app).get("/reviewers/rita").status_code == 404
    r = rita.post("/reviewer/profile", profile_form(skills="", expertise=""))
    assert r.status_code == 200 and "at least one skill" in r.get_data(as_text=True)


def test_completeness_counts_fields_only():
    assert rp.completeness({"full_name": "A B"}, [], [])[0] == round(100 / 9)
    full = {"full_name": "A B", "headline": "h", "bio": "b", "experience_level": "mid", "work_history": "w",
            "github_url": "https://github.com/a", "education": "e"}
    assert rp.completeness(full, ["x"], ["y"]) == (100, [])


# ---------------------------------------------------------------- privacy
def test_visibility_levels(app):
    rita = reviewer_browser(app, "rita")
    rita.post("/reviewer/profile", profile_form(profile_visibility="unlisted"))
    anon = Browser(app)
    assert anon.get("/reviewers/rita").status_code == 200              # public by link
    dev = Browser(app); dev.signup("devon")
    assert "Rita" not in dev.get("/reviewers").get_data(as_text=True)  # but not in the directory
    rita.post("/reviewer/profile", profile_form(profile_visibility="listed"))
    assert "Rita Reviewer" in dev.get("/reviewers").get_data(as_text=True)
    rita.post("/reviewer/profile", profile_form(profile_visibility="private"))
    assert anon.get("/reviewers/rita").status_code == 404
    assert "Rita" not in dev.get("/reviewers").get_data(as_text=True)


def test_developer_accounts_never_appear_as_reviewers(app):
    dev = Browser(app); dev.signup("devon")
    execute(app, "UPDATE profiles SET profile_visibility = 'listed' WHERE user_id = 1")
    assert Browser(app).get("/reviewers/devon").status_code == 404
    assert "/reviewers/devon" not in dev.get("/reviewers").get_data(as_text=True)


def test_optional_details_can_be_hidden_and_email_never_shown(app):
    rita = reviewer_browser(app, "rita")
    rita.post("/reviewer/profile", profile_form(work_history="SECRET-WORK", education="SECRET-EDU",
                                                bio="SECRET-BIO", pub_work="0", pub_education="0", pub_bio="0",
                                                pub_links="0", pub_experience="0"))
    anon = Browser(app)
    public = anon.get("/reviewers/rita").get_data(as_text=True)
    dev = Browser(app); dev.signup("devon")
    directory = dev.get("/reviewers").get_data(as_text=True)
    for html in (public, directory):
        assert "(self-reported):</span> Senior" not in html and "<b>Senior</b>" not in html
        for secret in ("SECRET-WORK", "SECRET-EDU", "SECRET-BIO", "github.com/rita"):
            assert secret not in html, secret
        assert "rita@example.com" not in html and "password" not in html.lower().replace("passwords", "")
    assert "Flask" in public                                           # skills stay public
    # the owner still sees and keeps everything
    assert "SECRET-WORK" in rita.get("/reviewer/profile").get_data(as_text=True)
    # and turning the flag back on reveals it
    rita.post("/reviewer/profile", profile_form(work_history="SECRET-WORK", pub_work="1"))
    assert "SECRET-WORK" in anon.get("/reviewers/rita").get_data(as_text=True)


def test_user_generated_text_is_escaped(app):
    rita = reviewer_browser(app, "rita")
    rita.post("/reviewer/profile", profile_form(display_name="<b>Rita</b>", bio="<script>alert(1)</script>",
                                                work_history="<img src=x onerror=alert(2)>",
                                                headline="<i>hi</i>"))
    dev = Browser(app); dev.signup("devon")
    public = Browser(app).get("/reviewers/rita").get_data(as_text=True)
    for html in (public, dev.get("/reviewers").get_data(as_text=True)):
        assert "<script>alert(1)" not in html and "<img src=x" not in html and "<b>Rita</b>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in public and "&lt;b&gt;Rita&lt;/b&gt;" in public
    page = Browser(app).get("/reviewers/rita").get_data(as_text=True)
    assert 'rel="noopener noreferrer nofollow ugc"' in page


def test_nothing_is_labelled_verified_on_profiles(app):
    rita = reviewer_browser(app, "rita")
    rita.post("/reviewer/profile", profile_form())
    html = Browser(app).get("/reviewers/rita").get_data(as_text=True).lower()
    assert "self-reported" in html
    assert "verified reviewer" not in html and "✓ verified" not in html


# ----------------------------------------------------------- the directory
@pytest.fixture
def crowd(app):
    names = [("anna", ["python", "flask"], ["security"], "senior"), ("bela", ["go"], ["devops"], "junior"),
             ("carl", ["python", "django"], ["api design"], "mid")]
    for n, skills, expertise, level in names:
        reviewer_browser(app, n)
        list_profile(app, n, skills, expertise, level=level)
        execute(app, "UPDATE profiles SET full_name = ? WHERE user_id = (SELECT id FROM users WHERE username = ?)",
                (n.capitalize() + " Example", n))
    return app


def names_in(html):
    return re.findall(r'href="/reviewers/([a-z0-9_.-]+)"', html)


def test_directory_filters(crowd):
    dev = Browser(crowd); dev.signup("devon")
    def find(**q):
        return sorted(set(names_in(dev.get("/reviewers", query_string=q).get_data(as_text=True))))
    assert find() == ["anna", "bela", "carl"]
    assert find(q="ann") == ["anna"] and find(q="BELA") == ["bela"]
    assert find(skill="python") == ["anna", "carl"] and find(skill="go") == ["bela", "carl"]      # substring match: "go" also matches "django"
    assert find(expertise="devops") == ["bela"]
    assert find(level="junior") == ["bela"] and find(level="senior", skill="python") == ["anna"]
    assert find(q="zzz") == []
    assert "No reviewers match" in dev.get("/reviewers", query_string={"q": "zzz"}).get_data(as_text=True)
    assert find(q="%") == [] and find(q="_") == []                     # LIKE wildcards are escaped


def test_directory_requires_login_and_paginates_server_side(app):
    assert Browser(app).get("/reviewers").status_code == 302
    for i in range(23):
        reviewer_browser(app, f"rev{i:02d}")
        list_profile(app, f"rev{i:02d}", ["python"])
    dev = Browser(app); dev.signup("devon")
    p1 = names_in(dev.get("/reviewers").get_data(as_text=True))
    p2 = names_in(dev.get("/reviewers", query_string={"page": 2}).get_data(as_text=True))
    p3 = names_in(dev.get("/reviewers", query_string={"page": 3}).get_data(as_text=True))
    assert (len(p1), len(p2), len(p3)) == (10, 10, 3)
    assert not set(p1) & set(p2) and "23 reviewers found" in dev.get("/reviewers").get_data(as_text=True)
    assert dev.get("/reviewers", query_string={"page": 999}).status_code == 200      # clamped
    assert dev.get("/reviewers", query_string={"page": "abc"}).status_code == 200


def test_reviewers_can_also_browse_the_directory(crowd):
    b = reviewer_browser(crowd, "zed")
    assert b.get("/reviewers").status_code == 200


# --------------------------------------------------------------- the picker
@pytest.fixture
def picker_world(crowd):
    alice = Browser(crowd); alice.signup("alice")
    task_id = create_task(alice, "Needs reviewers")
    fire(alice, task_id, "START"); fire(alice, task_id, "REQUEST_EVIDENCE")
    return crowd, alice, task_id


def select(alice, task_id, uid, action="add", **extra):
    return alice.post(f"/tasks/{task_id}/reviewers/select", {"reviewer_id": uid, "action": action, **extra},
                      follow_redirects=True)


def test_picker_search_select_and_save(picker_world):
    app, alice, task_id = picker_world
    page = alice.get(f"/tasks/{task_id}/reviewers", query_string={"skill": "python"}).get_data(as_text=True)
    assert "Anna Example" in page and "Carl Example" in page and "Bela Example" not in page
    assert "does not guarantee" in page and "self-reported" in page
    anna, carl, bela = (reviewer_ids(app, [n])[0] for n in ("anna", "carl", "bela"))
    page = select(alice, task_id, anna).get_data(as_text=True)
    assert "1 reviewer selected; 1 report required" in page
    page = select(alice, task_id, carl).get_data(as_text=True)
    assert "2 reviewers selected; 2 reports required" in page or "Selected reviewers: <span id=\"sel-count\">2" in page
    dup = select(alice, task_id, anna).get_data(as_text=True)
    assert "already selected" in dup
    page = alice.get(f"/tasks/{task_id}/reviewers", query_string={"required": 3}).get_data(as_text=True)
    assert "between 1 and 2" in page and "disabled" in page
    r = alice.post(f"/tasks/{task_id}/reviewer", {"reviewer_ids": [anna, carl], "required": "2"}, follow_redirects=True)
    assert "must submit a report" in r.get_data(as_text=True)
    assert sorted(x[0] for x in query(app, "SELECT u.username FROM review_assignments a JOIN users u ON u.id = a.reviewer_id")) == ["anna", "carl"]
    assert query(app, "SELECT required_approvals FROM workflow_instances")[0][0] == 2
    # the saved choice is what the picker now preselects
    assert "Selected reviewers: <span id=\"sel-count\">2</span>" in alice.get(f"/tasks/{task_id}/reviewers").get_data(as_text=True)


@pytest.mark.parametrize("k,n", [(1, 1), (2, 2), (2, 3)])
def test_picker_supports_existing_policies(picker_world, k, n):
    app, alice, task_id = picker_world
    reviewer_browser(app, "dina"); list_profile(app, "dina", ["go"])
    ids = reviewer_ids(app, ["anna", "bela", "carl", "dina"][:n])
    r = alice.post(f"/tasks/{task_id}/reviewer", {"reviewer_ids": ids, "required": str(k)}, follow_redirects=True)
    assert f"{k} of {n} must submit a report" in r.get_data(as_text=True)


def test_backend_rejects_invalid_selections(picker_world):
    app, alice, task_id = picker_world
    anna, bela = reviewer_ids(app, ["anna", "bela"])
    def attempt(ids, required="1"):
        r = alice.post(f"/tasks/{task_id}/reviewer", {"reviewer_ids": ids, "required": required}, follow_redirects=True)
        return r.get_data(as_text=True)
    assert "listed twice" in attempt([anna, anna]) or "twice" in attempt([anna, anna])
    assert "Required approvals must be between" in attempt([anna, bela], "3")
    assert "Required approvals must be between" in attempt([anna, bela], "0")
    assert "not valid" in attempt(["abc"]) or "not available" in attempt(["abc"])
    assert "not available in the directory" in attempt(["99999"])
    assert "at least one reviewer" in attempt([]).lower() or "Enter at least one" in attempt([])
    owner_id = str(query(app, "SELECT id FROM users WHERE username = 'alice'")[0][0])
    attempt([owner_id])                              # as a reviewer-role owner the route is closed to them
    from database.db import get_db_connection
    with app.app_context():
        conn = get_db_connection()
        try:
            with pytest.raises(rp.ProfileError, match="cannot choose yourself"):
                rp.selectable_reviewers(conn, [owner_id], int(owner_id))     # service-level self-review guard
        finally:
            conn.close()
    assert query(app, "SELECT COUNT(*) FROM review_assignments")[0][0] == 0
    # arbitrary names are not accepted any more
    r = alice.post(f"/tasks/{task_id}/reviewer", {"reviewers": "anna", "required": "1"}, follow_redirects=True)
    assert query(app, "SELECT COUNT(*) FROM review_assignments")[0][0] == 0


def test_unlisted_or_private_reviewers_cannot_be_assigned(picker_world):
    app, alice, task_id = picker_world
    reviewer_browser(app, "hidden"); list_profile(app, "hidden", ["python"], visibility="private")
    reviewer_browser(app, "noskill")
    execute(app, "UPDATE profiles SET profile_visibility = 'listed' WHERE user_id = (SELECT id FROM users WHERE username='noskill')")
    for name in ("hidden", "noskill"):
        r = alice.post(f"/tasks/{task_id}/reviewer", {"reviewer_ids": reviewer_ids(app, [name]), "required": "1"},
                       follow_redirects=True)
        assert "not available in the directory" in r.get_data(as_text=True)
        assert select(alice, task_id, reviewer_ids(app, [name])[0]).status_code == 200
    assert query(app, "SELECT COUNT(*) FROM review_assignments")[0][0] == 0


def test_picker_is_owner_only_and_state_limited(picker_world):
    app, alice, task_id = picker_world
    bob = Browser(app); bob.signup("bob")
    assert bob.get(f"/tasks/{task_id}/reviewers").status_code == 404
    assert bob.post(f"/tasks/{task_id}/reviewers/select", {"reviewer_id": "1", "action": "add"}).status_code == 404
    rev = reviewer_browser(app, "zed", skills=["python"])
    assert rev.get(f"/tasks/{task_id}/reviewers").status_code == 302          # reviewers are not developers
    assert alice.post(f"/tasks/{task_id}/reviewers/select", {"reviewer_id": "1", "action": "bogus"}).status_code == 400
    anna = reviewer_ids(app, ["anna"])
    alice.post(f"/tasks/{task_id}/reviewer", {"reviewer_ids": anna, "required": "1"})
    fire(alice, task_id, "SUBMIT_EVIDENCE")
    assert state(app, task_id) == "EVIDENCE_SUBMITTED"
    assert alice.get(f"/tasks/{task_id}/reviewers").status_code == 302         # sealed after submission
    r = alice.post(f"/tasks/{task_id}/reviewer", {"reviewer_ids": reviewer_ids(app, ["bela"]), "required": "1"},
                   follow_redirects=True)
    assert "preparing evidence" in r.get_data(as_text=True)


def test_picker_csrf_and_empty_state(picker_world):
    app, alice, task_id = picker_world
    assert alice.post(f"/tasks/{task_id}/reviewers/select", {"reviewer_id": "1", "action": "add"}, csrf=False).status_code == 400
    page = alice.get(f"/tasks/{task_id}/reviewers", query_string={"q": "nobody-like-this"}).get_data(as_text=True)
    assert "No matching reviewers" in page and "Select at least one reviewer" in page


def test_assigned_reviewer_flow_and_isolation_still_hold(picker_world):
    app, alice, task_id = picker_world
    anna, bela = (reviewer_ids(app, [n])[0] for n in ("anna", "bela"))
    alice.post(f"/tasks/{task_id}/reviewer", {"reviewer_ids": [anna], "required": "1"})
    fire(alice, task_id, "SUBMIT_EVIDENCE")
    anna_b = Browser(app); anna_b.post("/login?role=reviewer", {"email": "anna@example.com", "password": "correct-horse"})
    bela_b = Browser(app); bela_b.post("/login?role=reviewer", {"email": "bela@example.com", "password": "correct-horse"})
    assert "Needs reviewers" in anna_b.get("/review").get_data(as_text=True)
    assert "Needs reviewers" not in bela_b.get("/review").get_data(as_text=True)
    assert anna_b.get(f"/reviews/{task_id}").status_code == 200 and bela_b.get(f"/reviews/{task_id}").status_code == 404


# ------------------------------------------------------------- migration 4
def test_migration_4_is_additive_and_creates_profiles_for_reviewers(tmp_path):
    import sqlite3
    from app import init_db
    from database.db import get_db_connection
    from database.migrations import LATEST_VERSION, MIGRATIONS
    from tests.test_migrations import SCHEMA
    path = str(tmp_path / "v3.db")
    conn = sqlite3.connect(path); conn.row_factory = sqlite3.Row
    conn.executescript(open(SCHEMA, encoding="utf-8").read())
    for target, migrate in MIGRATIONS:
        if target <= 3:
            migrate(conn); conn.execute(f"PRAGMA user_version = {target}")
    conn.execute("INSERT INTO users (username, email, password, role) VALUES ('oldrev', 'o@example.com', 'x', 'reviewer')")
    conn.execute("INSERT INTO users (username, email, password, role) VALUES ('olddev', 'd@example.com', 'x', 'developer')")
    conn.commit(); conn.close()
    assert init_db(path) == LATEST_VERSION >= 4
    conn = get_db_connection(path)
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2
    rows = conn.execute("SELECT u.username, p.profile_visibility FROM profiles p JOIN users u ON u.id = p.user_id").fetchall()
    assert [(r[0], r[1]) for r in rows] == [("oldrev", "private")]      # private until the reviewer opts in
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(profiles)")}
    assert {"experience_level", "work_history", "pub_links", "github_url"} <= cols
    conn.close()
    assert init_db(path) == LATEST_VERSION                             # repeatable
