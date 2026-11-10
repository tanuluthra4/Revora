"""HTTP-level tests for Revisely review requests: consent, authorization, findings, sharing."""
import re

import pytest

from tests.helpers import execute, query
from tests.review_helpers import VERSION_FORM, make_project
from tests.test_routes import Browser, choose, fire, list_profile, state

PRIVATE = "PRIVATE-NOTE-xyz"
SECRET_EMAIL = "rev@example.com"


@pytest.fixture
def alice(app):
    b = Browser(app); assert b.signup("alice").status_code == 302
    return b


@pytest.fixture
def rev(app):
    b = Browser(app); assert b.signup("rev", SECRET_EMAIL, role="reviewer").status_code == 302
    list_profile(app, "rev"); return b


@pytest.fixture
def rev2(app):
    b = Browser(app); assert b.signup("rev2", "rev2@example.com", role="reviewer").status_code == 302
    list_profile(app, "rev2"); return b


def new_request(browser, app, goal="production", title="Review my API"):
    pid = make_project(app, query(app, "SELECT id FROM users WHERE username = 'alice'")[0][0], "Portfolio API")
    r = browser.post("/reviews/new", dict(VERSION_FORM, review_goal=goal, project_id=str(pid), title=title))
    assert r.status_code == 302, r.get_data(as_text=True)
    return int(re.search(r"/reviews/(\d+)", r.headers["Location"]).group(1))


def to_validating(alice, rev, app, tid, names=("rev",), required=1):
    fire(alice, tid, "START"); fire(alice, tid, "REQUEST_EVIDENCE")
    choose(alice, app, tid, list(names), required)
    fire(alice, tid, "SUBMIT_EVIDENCE")
    fire(rev, tid, "START_VALIDATION")
    assert state(app, tid) == "VALIDATING"


def report_form(**over):
    data = {"summary": "Solid small app.", "strengths": "Readable code.", "assessed": "Auth and routes.",
            "not_assessed": "Deployment.", "private_notes": PRIVATE, "consent_ack": "1", "consent_version": "v1",
            "f1_severity": "critical", "f1_category": "security", "f1_title": "Open redirect",
            "f1_explanation": "next parameter is followed blindly.", "f1_suggested_fix": "Validate the target."}
    data.update(over)
    return {k: v for k, v in data.items() if v is not None}


def reports(app):
    return query(app, "SELECT COUNT(*) FROM review_reports")[0][0]


# ----------------------------------------------------------------- end to end
def test_end_to_end_review_share_revoke_reshare(app, alice, rev):
    tid = new_request(alice, app)
    assert alice.get(f"/reviews/{tid}").status_code == 200
    to_validating(alice, rev, app, tid)
    resp = rev.post(f"/reviews/{tid}/report", report_form(), follow_redirects=True)
    assert "Report submitted" in resp.get_data(as_text=True)
    assert state(app, tid) == "REVIEWED"

    owner_page = alice.get(f"/reviews/{tid}").get_data(as_text=True)
    assert "Open redirect" in owner_page and "not</b> mean the project is ready" in owner_page
    assert PRIVATE not in owner_page and SECRET_EMAIL not in owner_page          # owner never sees private notes
    assert PRIVATE in rev.get(f"/reviews/{tid}").get_data(as_text=True)          # the author of the note does

    anon = Browser(app)
    wid = query(app, "SELECT id FROM workflow_instances WHERE task_id = ?", (tid,))[0][0]
    assert query(app, "SELECT COUNT(*) FROM share_links")[0][0] == 0               # private by default
    assert alice.post(f"/reviews/{tid}/share", {"round": "1"}).status_code == 302
    token = query(app, "SELECT token FROM share_links WHERE kind = 'report' AND revoked_at IS NULL")[0][0]
    page = anon.get(f"/r/{token}")
    html = page.get_data(as_text=True)
    assert page.status_code == 200 and "Open redirect" in html and "Solid small app." in html
    assert "critical" in html.lower() and "Not a certification" in html
    assert PRIVATE not in html and SECRET_EMAIL not in html and "Review my API" not in html
    assert "Is auth sound?" not in html                                            # the author's questions stay private
    assert "noindex" in page.headers.get("X-Robots-Tag", "")
    assert "separate accounts do not prove that reviewers are independent of the author" in html

    assert alice.post(f"/reviews/{tid}/unshare", {"round": "1"}).status_code == 302
    gone = anon.get(f"/r/{token}")
    gtext = gone.get_data(as_text=True)
    assert gone.status_code == 410
    for leak in ("Open redirect", "Solid small app.", "Portfolio API", "alice", "rev", "7fd1a60", "github.com/octocat"):
        assert leak not in gtext.replace("revoked", "").replace("review", ""), leak

    assert alice.post(f"/reviews/{tid}/share", {"round": "1"}).status_code == 302
    new_token = query(app, "SELECT token FROM share_links WHERE revoked_at IS NULL")[0][0]
    assert new_token != token
    assert anon.get(f"/r/{token}").status_code == 410                              # the old token stays dead
    assert anon.get(f"/r/{new_token}").status_code == 200
    assert anon.get("/r/not-a-token").status_code == 404


# ------------------------------------------------- consent: the UI is not the control
@pytest.mark.parametrize("override", [
    {"consent_ack": None},                      # checkbox field absent (direct POST)
    {"consent_ack": ""}, {"consent_ack": "0"}, {"consent_ack": "off"},
    {"consent_version": None}, {"consent_version": ""}, {"consent_version": "v0"},
    {"consent_ack": None, "consent_version": None},
])
def test_direct_post_without_valid_consent_is_refused_and_stores_nothing(app, alice, rev, override):
    tid = new_request(alice, app); to_validating(alice, rev, app, tid)
    before = query(app, "SELECT COUNT(*) FROM state_transitions")[0][0]
    resp = rev.post(f"/reviews/{tid}/report", report_form(**override), follow_redirects=True)
    assert "Report not submitted" in resp.get_data(as_text=True)
    assert reports(app) == 0
    assert query(app, "SELECT COUNT(*) FROM review_findings")[0][0] == 0
    assert query(app, "SELECT COUNT(*) FROM state_transitions")[0][0] == before
    assert state(app, tid) == "VALIDATING"
    assert query(app, "SELECT code FROM blocked_actions")[0][0] == "GUARD_FAILED"


def test_generic_event_route_cannot_bypass_the_report_form(app, alice, rev):
    tid = new_request(alice, app); to_validating(alice, rev, app, tid)
    rev.post(f"/tasks/{tid}/events/SUBMIT_REPORT", report_form(), follow_redirects=True)
    assert reports(app) == 0 and state(app, tid) == "VALIDATING"
    rev.post(f"/tasks/{tid}/events/COMPLETE_REVIEW", {}, follow_redirects=True)
    assert state(app, tid) == "VALIDATING"


def test_an_unacknowledged_report_can_never_become_public(app, alice, rev):
    tid = new_request(alice, app); to_validating(alice, rev, app, tid)
    rev.post(f"/reviews/{tid}/report", report_form(consent_ack=None))
    assert reports(app) == 0
    alice.post(f"/reviews/{tid}/share", {"round": "1"})
    assert query(app, "SELECT COUNT(*) FROM share_links")[0][0] == 0               # nothing to share
    # a DB-level attempt to store a report without acknowledgment is rejected too
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        execute(app, "INSERT INTO review_reports (workflow_id, round, reviewer_id, review_goal, summary, "
                     "strengths, assessed, not_assessed, consent_version, content_digest) "
                     "VALUES (1, 1, 2, 'production', 's', 's', 'a', 'n', 'v1', 'd')")


def test_consent_checkbox_is_rendered_and_required(app, alice, rev):
    tid = new_request(alice, app); to_validating(alice, rev, app, tid)
    html = rev.get(f"/reviews/{tid}").get_data(as_text=True)
    assert 'name="consent_ack"' in html and 'name="consent_version" value="v1"' in html
    assert re.search(r'name="consent_ack"[^>]*required|required[^>]*name="consent_ack"', html)
    assert "may be published on a public page" in html


def test_sharing_is_refused_before_the_review_is_complete(app, alice, rev, rev2):
    tid = new_request(alice, app)
    to_validating(alice, rev, app, tid, names=("rev", "rev2"), required=2)
    rev.post(f"/reviews/{tid}/report", report_form())
    assert state(app, tid) == "VALIDATING"
    alice.post(f"/reviews/{tid}/share", {"round": "1"})
    assert query(app, "SELECT COUNT(*) FROM share_links")[0][0] == 0
    rev2.post(f"/reviews/{tid}/report", report_form(summary="Second opinion."))
    assert state(app, tid) == "REVIEWED"
    alice.post(f"/reviews/{tid}/share", {"round": "1"})
    token = query(app, "SELECT token FROM share_links")[0][0]
    html = Browser(app).get(f"/r/{token}").get_data(as_text=True)
    assert "Solid small app." in html and "Second opinion." in html
    alice.post(f"/reviews/{tid}/share", {"round": "9"})                            # a round that does not exist
    assert query(app, "SELECT COUNT(*) FROM share_links")[0][0] == 1


# -------------------------------------------------------------- authorization
def test_access_matrix(app, alice, rev, rev2):
    bob = Browser(app); bob.signup("bob")
    tid = new_request(alice, app)
    to_validating(alice, rev, app, tid)
    assert rev2.get(f"/reviews/{tid}").status_code == 404                           # unassigned reviewer
    assert bob.get(f"/reviews/{tid}").status_code == 404                            # another developer
    assert Browser(app).get(f"/reviews/{tid}").status_code == 302                   # logged out
    assert rev2.post(f"/reviews/{tid}/report", report_form()).status_code == 404
    assert bob.post(f"/reviews/{tid}/report", report_form()).status_code == 404
    assert bob.post(f"/reviews/{tid}/share", {"round": "1"}).status_code == 404
    assert bob.post(f"/reviews/{tid}/version", dict(VERSION_FORM)).status_code == 404
    r = alice.post(f"/reviews/{tid}/report", report_form(), follow_redirects=True)  # owner cannot report
    assert "UNAUTHORIZED_ACTOR" in r.get_data(as_text=True) and reports(app) == 0
    assert rev.post(f"/reviews/{tid}/share", {"round": "1"}).status_code in (302, 403)
    assert query(app, "SELECT COUNT(*) FROM share_links")[0][0] == 0


def test_reviewers_do_not_see_each_others_reports(app, alice, rev, rev2):
    tid = new_request(alice, app)
    to_validating(alice, rev, app, tid, names=("rev", "rev2"), required=2)
    fire(rev2, tid, "START_VALIDATION")
    rev.post(f"/reviews/{tid}/report", report_form(summary="REV-ONE-TEXT", private_notes="ONE-PRIVATE"))
    assert "REV-ONE-TEXT" not in rev2.get(f"/reviews/{tid}").get_data(as_text=True)
    rev2.post(f"/reviews/{tid}/report", report_form(summary="REV-TWO-TEXT", private_notes="TWO-PRIVATE"))
    page = rev2.get(f"/reviews/{tid}").get_data(as_text=True)
    assert "REV-ONE-TEXT" not in page and "ONE-PRIVATE" not in page and "TWO-PRIVATE" in page
    owner = alice.get(f"/reviews/{tid}").get_data(as_text=True)
    assert "REV-ONE-TEXT" in owner and "REV-TWO-TEXT" in owner and "PRIVATE" not in owner


def test_reviewer_account_cannot_request_a_review_of_its_own(app, rev):
    pid = make_project(app, 1)
    r = rev.post("/reviews/new", dict(VERSION_FORM, project_id=str(pid), title="mine"))
    assert r.status_code == 302 and "/review" in r.headers["Location"]
    assert query(app, "SELECT COUNT(*) FROM workflow_instances")[0][0] == 0


def test_project_is_required_and_must_be_the_owners(app, alice):
    bob = Browser(app); bob.signup("bob")
    bobs = make_project(app, query(app, "SELECT id FROM users WHERE username = 'bob'")[0][0])
    for pid in ("", "abc", str(bobs), "9999"):
        r = alice.post("/reviews/new", dict(VERSION_FORM, project_id=pid, title="t"), follow_redirects=True)
        assert r.status_code == 200
    assert query(app, "SELECT COUNT(*) FROM workflow_instances")[0][0] == 0


@pytest.mark.parametrize("bad", [{"commit_sha": "not-hex"}, {"repo_url": "https://u:p@github.com/x/y"},
                                 {"repo_url": "ftp://x.com/y"}, {"tech_stack": ""},
                                 {"review_goal": "fame"}, {"questions": "token ghp_abcdefghijklmnopqrstuv"}])
def test_version_validation_and_no_credentials(app, alice, bad):
    pid = make_project(app, 1)
    alice.post("/reviews/new", dict(VERSION_FORM, project_id=str(pid), title="t", **bad))
    assert query(app, "SELECT COUNT(*) FROM workflow_instances")[0][0] == 0


def test_submitted_version_cannot_be_edited(app, alice, rev):
    tid = new_request(alice, app); to_validating(alice, rev, app, tid)
    r = alice.post(f"/reviews/{tid}/version", dict(VERSION_FORM, commit_sha="abcdef1"), follow_redirects=True)
    assert "cannot be edited" in r.get_data(as_text=True) or "locked" in r.get_data(as_text=True)
    assert query(app, "SELECT commit_sha FROM submission_versions")[0][0] == VERSION_FORM["commit_sha"]


def test_requests_with_reports_cannot_be_deleted(app, alice, rev):
    tid = new_request(alice, app); to_validating(alice, rev, app, tid)
    rev.post(f"/reviews/{tid}/report", report_form())
    alice.post(f"/tasks/{tid}/delete")
    assert query(app, "SELECT COUNT(*) FROM review_reports")[0][0] == 1


# ------------------------------------------------------------------- findings
def finding_id(app):
    return query(app, "SELECT id FROM review_findings")[0][0]


def test_finding_lifecycle_and_who_may_do_what(app, alice, rev, rev2):
    tid = new_request(alice, app); to_validating(alice, rev, app, tid)
    rev.post(f"/reviews/{tid}/report", report_form())
    fid = finding_id(app)
    up = f"/findings/{fid}/update"
    # the reviewer cannot mark their own finding fixed; a stranger sees nothing
    assert "cannot move" in rev.post(up, {"to_status": "fixed", "note": "done"}, follow_redirects=True).get_data(as_text=True)
    assert rev2.post(up, {"to_status": "reopened", "note": "no"}).status_code == 404
    assert Browser(app).post(up, {"to_status": "fixed", "note": "x"}, csrf=False).status_code == 400
    assert alice.post(up, {"to_status": "confirmed", "note": "I say so"}, follow_redirects=True) and \
        query(app, "SELECT COUNT(*) FROM finding_updates")[0][0] == 0
    alice.post(up, {"to_status": "fixed", "note": "Validated redirect targets.", "commit_ref": "abc1234"})
    rev.post(up, {"to_status": "reopened", "note": "Still bypassable with //evil.com"})
    alice.post(up, {"to_status": "fixed", "note": "Blocked protocol-relative URLs."})
    rev.post(up, {"to_status": "confirmed", "note": "Verified the fix."})
    steps = [(r["actor_role"], r["from_status"], r["to_status"]) for r in
             query(app, "SELECT * FROM finding_updates ORDER BY seq")]
    assert steps == [("owner", "open", "fixed"), ("reviewer", "fixed", "reopened"),
                     ("owner", "reopened", "fixed"), ("reviewer", "fixed", "confirmed")]
    assert alice.post(up, {"to_status": "wont_fix", "note": "changed my mind"}).status_code == 302
    assert query(app, "SELECT COUNT(*) FROM finding_updates")[0][0] == 4            # confirmed is final
    from tests.test_review_engine import replay
    assert replay(app, 1)["ok"]


def test_tampering_with_a_finding_history_is_detected(app, alice, rev):
    tid = new_request(alice, app); to_validating(alice, rev, app, tid)
    rev.post(f"/reviews/{tid}/report", report_form())
    alice.post(f"/findings/{finding_id(app)}/update", {"to_status": "acknowledged", "note": "Will do."})
    from tests.test_review_engine import get_db_connection_for
    from services.integrity import verify_chain
    with get_db_connection_for(app) as conn:
        conn.execute("DROP TRIGGER finding_updates_no_update")
        conn.execute("UPDATE finding_updates SET note = 'edited later'")
        result = verify_chain(conn, 1)
    assert not result["ok"] and result["error"]["code"] == "FINDING_HISTORY_INVALID"


def test_unresolved_and_critical_findings_stay_visible_on_the_share(app, alice, rev):
    tid = new_request(alice, app); to_validating(alice, rev, app, tid)
    rev.post(f"/reviews/{tid}/report", report_form(unresolved="Rate limiting was not examined."))
    alice.post(f"/reviews/{tid}/share", {"round": "1"})
    token = query(app, "SELECT token FROM share_links")[0][0]
    html = Browser(app).get(f"/r/{token}").get_data(as_text=True)
    assert "Open redirect" in html and "Rate limiting was not examined." in html and "Critical" in html


# --------------------------------------------------------- follow-up versions
def test_follow_up_version_and_per_version_sharing(app, alice, rev):
    tid = new_request(alice, app); to_validating(alice, rev, app, tid)
    rev.post(f"/reviews/{tid}/report", report_form())
    fire(alice, tid, "NEW_VERSION")
    assert state(app, tid) == "IN_PROGRESS"
    alice.post(f"/reviews/{tid}/version", dict(VERSION_FORM, commit_sha="1234567"))
    fire(alice, tid, "REQUEST_EVIDENCE"); fire(alice, tid, "SUBMIT_EVIDENCE"); fire(rev, tid, "START_VALIDATION")
    prior = finding_id(app)
    rev.post(f"/reviews/{tid}/report", report_form(summary="Second pass.", f1_prior_finding_id=str(prior),
                                                    f1_title="Redirect partially fixed"))
    assert state(app, tid) == "REVIEWED"
    alice.post(f"/reviews/{tid}/share", {"round": "2"})
    token = query(app, "SELECT token FROM share_links")[0][0]
    html = Browser(app).get(f"/r/{token}").get_data(as_text=True)
    assert "Second pass." in html and "Solid small app." not in html                # ONE version only
    assert "1234567" in html
    assert query(app, "SELECT prior_finding_id FROM review_findings WHERE id = 2")[0][0] == prior


# ----------------------------------------------------------- legacy links
def test_legacy_proof_links_still_work_after_migration(tmp_path):
    from app import create_app
    from tests.test_migration5 import build_v4_database
    path = str(tmp_path / "v4.db"); build_v4_database(path)
    app = create_app({"TESTING": True, "DATABASE": path, "SECRET_KEY": "k"})
    client = app.test_client()
    ok = client.get("/proof/legacytoken")
    assert ok.status_code == 200 and "Legacy verified" in ok.get_data(as_text=True)
    gone = client.get("/proof/revokedtoken")
    assert gone.status_code == 410 and "Legacy" not in gone.get_data(as_text=True)
    assert client.get("/proof/nope").status_code == 404
