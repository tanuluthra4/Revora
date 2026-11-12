"""Progress across versions: derived counts, follow-up links, and who may see what."""
from tests.helpers import query
from tests.review_helpers import VERSION_FORM
from tests.test_review_routes import (alice, finding_id, new_request, rev, report_form,  # noqa: F401  (fixtures)
                                      to_validating)
from tests.test_routes import Browser, bob, create_task, fire  # noqa: F401


def two_rounds(alice, rev, app):
    tid = new_request(alice, app); to_validating(alice, rev, app, tid)
    rev.post(f"/reviews/{tid}/report", report_form(f1_title="Open redirect"))
    fid = finding_id(app)
    alice.post(f"/findings/{fid}/update", {"to_status": "fixed", "note": "patched"})
    rev.post(f"/findings/{fid}/update", {"to_status": "confirmed", "note": "checked"})
    fire(alice, tid, "NEW_VERSION")
    alice.post(f"/reviews/{tid}/version", dict(VERSION_FORM, commit_sha="1234567"))
    fire(alice, tid, "REQUEST_EVIDENCE"); fire(alice, tid, "SUBMIT_EVIDENCE"); fire(rev, tid, "START_VALIDATION")
    rev.post(f"/reviews/{tid}/report", report_form(f1_title="New weakness", f1_prior_finding_id=str(fid)))
    return tid, fid


def test_progress_shows_counts_and_follow_up_links(app, alice, rev):
    tid, fid = two_rounds(alice, rev, app)
    html = alice.get(f"/reviews/{tid}/progress").get_data(as_text=True)
    assert "2</b> finding" in html and "across 2 versions" in html
    assert "1 resolved (verified by the reviewer)" in html and "1 unresolved" in html
    assert "follow-up to &ldquo;Open redirect&rdquo; from version 1" in html
    assert "followed up in version 2" in html
    assert "not a score or a grade" in html
    assert query(app, "SELECT COUNT(*) FROM finding_updates")[0][0] == 2          # read-only page


def test_progress_is_limited_to_the_two_parties(app, alice, bob, rev):
    tid, _ = two_rounds(alice, rev, app)
    assert bob.get(f"/reviews/{tid}/progress").status_code == 404
    assert Browser(app).get(f"/reviews/{tid}/progress").status_code in (302, 401, 403)
    other = Browser(app); other.signup("reviewer9", "r9@example.com", role="reviewer")
    assert other.get(f"/reviews/{tid}/progress").status_code in (302, 404)


def test_reviewer_sees_only_their_own_findings_and_no_private_notes(app, alice, rev):
    tid, _ = two_rounds(alice, rev, app)
    html = rev.get(f"/reviews/{tid}/progress").get_data(as_text=True)
    assert "your own reports only" in html and "Open redirect" in html
    assert "Private note" not in html


def test_empty_state_and_verification_mode_requests(app, alice):
    tid = new_request(alice, app)
    assert "No findings have been recorded yet" in alice.get(f"/reviews/{tid}/progress").get_data(as_text=True)
    task = create_task(alice, "Legacy task")
    assert alice.get(f"/reviews/{task}/progress").status_code == 404
