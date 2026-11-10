"""The full student/reviewer journey over HTTP, on a throwaway database and test accounts only."""
import re

from tests.helpers import query
from tests.review_helpers import VERSION_FORM
from tests.test_routes import Browser, choose, fire, list_profile, state

PRIVATE = "E2E-PRIVATE-NOTE"


def report(**over):
    data = {"summary": "E2E summary text.", "strengths": "Clear structure.", "assessed": "Routes and data layer.",
            "not_assessed": "I did not run the code or review deployment.", "private_notes": PRIVATE,
            "consent_ack": "1", "consent_version": "v1", "f1_severity": "high", "f1_category": "code-quality",
            "f1_title": "Missing rate limit", "f1_explanation": "Login can be guessed at without limit.",
            "f1_suggested_fix": "Add a failed-attempt throttle."}
    data.update(over)
    return data


def test_complete_journey_and_isolation(app):
    # 1-2. register a student, then log in from a fresh browser
    assert Browser(app).signup("student1", "student1@example.com").status_code == 302
    student = Browser(app)
    assert student.login("student1@example.com").status_code == 302
    assert student.get("/dashboard").status_code == 200

    reviewer_signup = Browser(app)
    assert reviewer_signup.signup("reviewer1", "reviewer1@example.com", role="reviewer").status_code == 302
    list_profile(app, "reviewer1")

    # 3. create a project
    r = student.post("/projects/new", {"title": "E2E Portfolio", "description": "d", "status": "Active"})
    assert r.status_code == 302
    project_id = query(app, "SELECT id FROM projects WHERE title = 'E2E Portfolio'")[0][0]

    # 4. submit the project for review (request + version 1)
    r = student.post("/reviews/new", dict(VERSION_FORM, project_id=str(project_id), title="E2E review",
                                          review_goal="interview"))
    assert r.status_code == 302
    task_id = int(re.search(r"/reviews/(\d+)", r.headers["Location"]).group(1))
    fire(student, task_id, "START"); fire(student, task_id, "REQUEST_EVIDENCE")

    # 5. select a reviewer from the directory, then submit the version
    assert "reviewer1" in student.get("/reviewers").get_data(as_text=True)
    assert choose(student, app, task_id, ["reviewer1"], 1).status_code == 302
    fire(student, task_id, "SUBMIT_EVIDENCE")
    assert state(app, task_id) == "EVIDENCE_SUBMITTED"

    # 6-7. log in as the reviewer and open the assigned review (it shows the chosen objective)
    reviewer = Browser(app)
    assert reviewer.post("/login?role=reviewer", {"email": "reviewer1@example.com",
                                                  "password": "correct-horse"}).status_code == 302
    assert task_id and "E2E review" in reviewer.get("/review").get_data(as_text=True)
    page = reviewer.get(f"/reviews/{task_id}")
    assert page.status_code == 200 and "Interview preparation" in page.get_data(as_text=True)
    fire(reviewer, task_id, "START_VALIDATION")

    # 8. submit a structured report
    resp = reviewer.post(f"/reviews/{task_id}/report", report(), follow_redirects=True)
    body = resp.get_data(as_text=True)
    assert "Report submitted" in body, re.findall(r"Report not submitted[^<]*", body)
    assert state(app, task_id) == "REVIEWED"

    # 9-10. back as the student: the report is visible, private notes are not
    html = student.get(f"/reviews/{task_id}").get_data(as_text=True)
    assert "E2E summary text." in html and "Missing rate limit" in html and PRIVATE not in html
    fid = query(app, "SELECT id FROM review_findings")[0][0]
    update = f"/findings/{fid}/update"

    # negative: the student cannot verify their own finding, and an unrelated reviewer cannot touch it
    student.post(update, {"to_status": "confirmed", "note": "I verified it myself"})
    assert query(app, "SELECT COUNT(*) FROM finding_updates")[0][0] == 0
    outsider_reviewer = Browser(app); outsider_reviewer.signup("reviewer2", "r2@example.com", role="reviewer")
    assert outsider_reviewer.post(update, {"to_status": "confirmed", "note": "not mine"}).status_code == 404

    # 11-12. In progress, then fixed - awaiting verification
    student.post(update, {"to_status": "acknowledged", "note": "Starting on the throttle."})
    student.post(update, {"to_status": "fixed", "note": "Added throttle in auth.", "commit_ref": "abc1234"})
    assert "Fixed — awaiting verification" in student.get(f"/reviews/{task_id}").get_data(as_text=True)

    # 13-14. the original reviewer verifies the fix
    reviewer.post(update, {"to_status": "confirmed", "note": "Checked: 11th attempt is refused."})
    assert [r[0] for r in query(app, "SELECT to_status FROM finding_updates ORDER BY seq")] == \
        ["acknowledged", "fixed", "confirmed"]
    assert "Resolved — verified by the reviewer" in reviewer.get(f"/reviews/{task_id}").get_data(as_text=True)
    student.post(update, {"to_status": "wont_fix", "note": "changed my mind"})          # confirmed is final
    assert query(app, "SELECT COUNT(*) FROM finding_updates")[0][0] == 3

    # 15-16. a new version; history stays intact
    old_digest = query(app, "SELECT content_digest FROM review_reports")[0][0]
    fire(student, task_id, "NEW_VERSION")
    student.post(f"/reviews/{task_id}/version", dict(VERSION_FORM, commit_sha="fedcba9", review_goal="interview"))
    assert [r[0] for r in query(app, "SELECT round FROM submission_versions ORDER BY round")] == [1, 2]
    assert query(app, "SELECT content_digest FROM review_reports WHERE round = 1")[0][0] == old_digest
    assert query(app, "SELECT COUNT(*) FROM review_findings")[0][0] == 1
    assert "E2E summary text." in student.get(f"/reviews/{task_id}").get_data(as_text=True)   # still readable

    # 17-18. share version 1 and open it with no login
    assert query(app, "SELECT COUNT(*) FROM share_links")[0][0] == 0                  # private by default
    student.post(f"/reviews/{task_id}/share", {"round": "1"})
    token = query(app, "SELECT token FROM share_links WHERE revoked_at IS NULL")[0][0]
    anon = Browser(app)
    public = anon.get(f"/r/{token}")
    text = public.get_data(as_text=True)
    assert public.status_code == 200 and "E2E summary text." in text and "Missing rate limit" in text
    assert "Not a certification" in text and "fedcba9" not in text                    # only version 1
    for secret in (PRIVATE, "student1@example.com", "reviewer1@example.com", "correct-horse", "_csrf"):
        assert secret not in text

    # 19-20. revoke; the token now exposes nothing
    student.post(f"/reviews/{task_id}/unshare", {"round": "1"})
    gone = anon.get(f"/r/{token}")
    assert gone.status_code == 410
    for leak in ("E2E summary text.", "Missing rate limit", "E2E Portfolio", "student1"):
        assert leak not in gone.get_data(as_text=True)
    assert anon.get("/r/not-a-real-token").status_code == 404

    # isolation: a different student and an unrelated reviewer cannot reach the request
    other = Browser(app); other.signup("student2", "student2@example.com")
    assert other.get(f"/reviews/{task_id}").status_code == 404
    assert other.post(f"/reviews/{task_id}/share", {"round": "1"}).status_code == 404
    assert outsider_reviewer.get(f"/reviews/{task_id}").status_code == 404
    assert outsider_reviewer.post(f"/reviews/{task_id}/report", report()).status_code == 404
    assert other.post(f"/reviews/{task_id}/version", dict(VERSION_FORM)).status_code == 404
