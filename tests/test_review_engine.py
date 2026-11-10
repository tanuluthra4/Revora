"""Review-mode workflow: reports, consent, quorum vs readiness, versions, replay."""
import pytest

from automata.states import WorkflowEvent as E
from database.db import get_db_connection
from services.automata_engine import TransitionBlocked
from services.integrity import verify_chain
from tests.helpers import assign_many, execute, fire, make_task, make_user, query, vote
from tests.review_helpers import (count, finding, make_review_request, report_extra, submit_report,
                                  to_validating)


@pytest.fixture
def setup(app):
    dev = make_user(app, "devone")
    r1, r2, r3 = (make_user(app, n, "reviewer") for n in ("rev_one", "rev_two", "rev_three"))
    task, wid = make_review_request(app, dev)
    return app, dev, r1, r2, r3, wid


def state(app, wid):
    return query(app, "SELECT current_state FROM workflow_instances WHERE id = ?", (wid,))[0][0]


def replay(app, wid):
    with app.app_context():
        conn = get_db_connection()
        try:
            return verify_chain(conn, wid)
        finally:
            conn.close()


# ------------------------------------------------------------ happy path
def test_two_of_two_reports_complete_the_review_and_replay_is_clean(setup):
    app, dev, r1, r2, r3, wid = setup
    to_validating(app, wid, dev, [r1, r2], required=2)
    assert submit_report(app, wid, r1)["to_state"] == "VALIDATING"
    step = submit_report(app, wid, r2)
    assert step["to_state"] == "REVIEWED"
    assert count(app, "review_reports") == 2 and count(app, "review_findings") == 2
    result = replay(app, wid)
    assert result["ok"], result
    assert result["replayed_state"] == "REVIEWED"


def test_quorum_completion_never_means_readiness(setup):
    app, dev, r1, r2, r3, wid = setup
    to_validating(app, wid, dev, [r1], required=1)
    submit_report(app, wid, r1, findings=[finding(severity="critical", title="Auth bypass")])
    row = query(app, "SELECT current_state, result, completed_at FROM workflow_instances WHERE id = ?", (wid,))[0]
    assert row["current_state"] == "REVIEWED"
    assert row["result"] is None and row["completed_at"] is None          # no verdict is recorded
    assert query(app, "SELECT status FROM tasks")[0][0] != "Completed"     # the task is not "done"
    assert query(app, "SELECT COUNT(*) FROM state_transitions WHERE event IN ('APPROVE','REJECT')")[0][0] == 0
    reasons = " ".join(r[0] or "" for r in query(app, "SELECT reason FROM state_transitions"))
    for word in ("verified", "approved", "ready"):
        assert word not in reasons.lower().replace("not a readiness", "")


def test_open_critical_finding_does_not_block_completion_but_stays_visible(setup):
    app, dev, r1, r2, r3, wid = setup
    to_validating(app, wid, dev, [r1])
    submit_report(app, wid, r1, findings=[finding(severity="critical")])
    assert query(app, "SELECT severity FROM review_findings")[0][0] == "critical"
    assert state(app, wid) == "REVIEWED"


def test_new_version_increments_round_and_carries_reviewers_not_reports(setup):
    app, dev, r1, r2, r3, wid = setup
    to_validating(app, wid, dev, [r1])
    submit_report(app, wid, r1)
    assert fire(app, wid, E.NEW_VERSION, dev)["to_state"] == "IN_PROGRESS"
    wf = query(app, "SELECT round, current_state FROM workflow_instances WHERE id = ?", (wid,))[0]
    assert wf["round"] == 2
    v = query(app, "SELECT round, locked_at, commit_sha FROM submission_versions ORDER BY round")
    assert [r["round"] for r in v] == [1, 2] and v[0]["locked_at"] and v[1]["locked_at"] is None
    assert [r[0] for r in query(app, "SELECT reviewer_id FROM review_assignments WHERE round = 2")] == [r1]
    assert count(app, "review_reports", "round = 2") == 0
    # second version goes through the same path and the whole chain still replays
    fire(app, wid, E.REQUEST_EVIDENCE, dev)
    fire(app, wid, E.SUBMIT_EVIDENCE, dev)
    fire(app, wid, E.START_VALIDATION, r1)
    submit_report(app, wid, r1)
    assert state(app, wid) == "REVIEWED"
    assert replay(app, wid)["ok"]


def test_only_owner_can_start_a_new_version_and_only_when_reviewed(setup):
    app, dev, r1, r2, r3, wid = setup
    to_validating(app, wid, dev, [r1])
    with pytest.raises(TransitionBlocked) as e:
        fire(app, wid, E.NEW_VERSION, dev)
    assert e.value.code == "INVALID_TRANSITION"
    submit_report(app, wid, r1)
    with pytest.raises(TransitionBlocked) as e:
        fire(app, wid, E.NEW_VERSION, r1)
    assert e.value.code == "UNAUTHORIZED_ACTOR"
    with pytest.raises(TransitionBlocked) as e:
        fire(app, wid, E.COMPLETE_REVIEW, dev)
    assert e.value.code in ("INVALID_TRANSITION", "UNAUTHORIZED_ACTOR")


# ------------------------------------------------------- consent (B2 safeguard)
@pytest.mark.parametrize("kw", [
    {"consent": False},                                   # checkbox absent
    {"consent": True, "version": ""},                     # version blanked
    {"consent": True, "version": "v0"},                   # stale / forged version
    {"consent": True, "version": "v999"},
])
def test_report_without_valid_acknowledgment_is_refused_and_nothing_is_stored(setup, kw):
    app, dev, r1, r2, r3, wid = setup
    to_validating(app, wid, dev, [r1])
    before = count(app, "state_transitions")
    with pytest.raises(TransitionBlocked) as e:
        submit_report(app, wid, r1, **kw)
    assert e.value.code == "GUARD_FAILED" and "acknowledge" in str(e.value)
    assert count(app, "review_reports") == 0 and count(app, "review_findings") == 0
    assert count(app, "state_transitions") == before
    assert state(app, wid) == "VALIDATING"
    blocked = query(app, "SELECT code, event FROM blocked_actions")
    assert [(b["code"], b["event"]) for b in blocked] == [("GUARD_FAILED", "SUBMIT_REPORT")]


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", None])
def test_falsy_acknowledgment_values_are_not_consent(setup, value):
    app, dev, r1, r2, r3, wid = setup
    to_validating(app, wid, dev, [r1])
    extra = report_extra()
    extra["consent_ack"] = value
    with pytest.raises(TransitionBlocked):
        fire(app, wid, E.SUBMIT_REPORT, r1, None, extra)
    assert count(app, "review_reports") == 0


def test_consent_is_recorded_and_sealed(setup):
    app, dev, r1, r2, r3, wid = setup
    to_validating(app, wid, dev, [r1])
    submit_report(app, wid, r1)
    row = query(app, "SELECT consent_ack_at, consent_version FROM review_reports")[0]
    assert row["consent_ack_at"] and row["consent_version"] == "v1"
    meta = query(app, "SELECT meta FROM state_transitions WHERE event = 'SUBMIT_REPORT'")[0][0]
    assert '"consent_version":"v1"' in meta


def test_removing_consent_from_a_stored_report_is_detected_by_replay(setup):
    app, dev, r1, r2, r3, wid = setup
    to_validating(app, wid, dev, [r1])
    submit_report(app, wid, r1)
    with get_db_connection_for(app) as conn:
        conn.execute("DROP TRIGGER review_reports_no_update")
        conn.execute("UPDATE review_reports SET consent_version = 'vX'")
        result = verify_chain(conn, wid)
    assert not result["ok"] and result["error"]["code"] in ("CONSENT_MISSING", "REPORT_MISMATCH")


class get_db_connection_for:
    def __init__(self, app):
        self.app = app

    def __enter__(self):
        self.ctx = self.app.app_context(); self.ctx.push()
        self.conn = get_db_connection()
        return self.conn

    def __exit__(self, *a):
        self.conn.close(); self.ctx.pop()


# ------------------------------------------------ self-review and authorization
def test_owner_cannot_report_or_be_assigned_to_their_own_request(setup):
    app, dev, r1, r2, r3, wid = setup
    # a developer-role account is not a reviewer at all
    to_validating(app, wid, dev, [r1])
    with pytest.raises(TransitionBlocked) as e:
        submit_report(app, wid, dev)
    assert e.value.code == "UNAUTHORIZED_ACTOR"


def test_account_that_is_also_the_owner_is_blocked_even_if_it_holds_the_reviewer_role(app):
    owner = make_user(app, "dualaccount", "reviewer")
    other = make_user(app, "rev_other", "reviewer")
    _, wid = make_review_request(app, owner)
    fire(app, wid, E.START, owner)
    fire(app, wid, E.REQUEST_EVIDENCE, owner)
    from services.evidence_service import EvidenceError
    with pytest.raises(EvidenceError):
        assign_many(app, wid, owner, [owner, other], 1)          # blocked at assignment
    # forced into the assignment table by someone with database access: blocked at submission
    execute(app, "INSERT INTO review_assignments (workflow_id, round, reviewer_id) VALUES (?, 1, ?)", (wid, owner))
    execute(app, "UPDATE workflow_instances SET required_approvals = 1 WHERE id = ?", (wid,))
    with pytest.raises(TransitionBlocked) as e:
        fire(app, wid, E.SUBMIT_EVIDENCE, owner)
    assert "your own work" in str(e.value)


def test_unassigned_reviewer_and_second_report_are_refused(setup):
    app, dev, r1, r2, r3, wid = setup
    to_validating(app, wid, dev, [r1, r2], required=2)
    with pytest.raises(TransitionBlocked) as e:
        submit_report(app, wid, r3)
    assert e.value.code == "UNAUTHORIZED_ACTOR"
    submit_report(app, wid, r1)
    with pytest.raises(TransitionBlocked) as e:
        submit_report(app, wid, r1)
    assert e.value.code == "GUARD_FAILED"
    assert count(app, "review_reports") == 1


def test_reviewer_who_reported_cannot_be_replaced(setup):
    app, dev, r1, r2, r3, wid = setup
    to_validating(app, wid, dev, [r1, r2], required=2)
    submit_report(app, wid, r1)
    with pytest.raises(TransitionBlocked):
        fire(app, wid, E.REASSIGN_REVIEWER, dev, None, {"remove": "rev_one", "add": "rev_three"})


# ----------------------------------------------------------------- mode guards
def test_review_events_are_refused_in_verification_mode_and_vice_versa(app):
    dev = make_user(app, "devtwo"); rev = make_user(app, "rev_x", "reviewer")
    _, vwid = make_task(app, dev)
    from tests.helpers import advance_to_validating
    advance_to_validating(app, vwid, dev, rev)
    with pytest.raises(TransitionBlocked) as e:
        submit_report(app, vwid, rev)
    assert e.value.code == "INVALID_TRANSITION"
    _, rwid = make_review_request(app, dev)
    to_validating(app, rwid, dev, [rev])
    with pytest.raises(TransitionBlocked) as e:
        vote(app, rwid, rev)
    assert e.value.code == "INVALID_TRANSITION"
    assert state(app, rwid) == "VALIDATING"


def test_legacy_verification_flow_is_unchanged(app):
    dev = make_user(app, "devthree")
    r1 = make_user(app, "rev_y", "reviewer"); r2 = make_user(app, "rev_z", "reviewer")
    _, wid = make_task(app, dev)
    from tests.helpers import advance_to_validating
    advance_to_validating(app, wid, dev, [r1, r2], required=2)
    vote(app, wid, r1); assert vote(app, wid, r2)["to_state"] == "VERIFIED"
    assert replay(app, wid)["ok"]
    assert query(app, "SELECT review_mode FROM workflow_instances WHERE id = ?", (wid,))[0][0] == "verification"


# ------------------------------------------------------------ versions + replay
def test_review_submission_requires_a_complete_version(setup):
    app, dev, r1, r2, r3, wid = setup
    fire(app, wid, E.START, dev); fire(app, wid, E.REQUEST_EVIDENCE, dev)
    assign_many(app, wid, dev, [r1], 1)
    execute(app, "DELETE FROM submission_versions WHERE workflow_id = ?", (wid,))
    with pytest.raises(TransitionBlocked) as e:
        fire(app, wid, E.SUBMIT_EVIDENCE, dev)
    assert e.value.code == "GUARD_FAILED"


def test_submitted_version_is_locked_and_tampering_is_detected(setup):
    app, dev, r1, r2, r3, wid = setup
    to_validating(app, wid, dev, [r1])
    import sqlite3
    with get_db_connection_for(app) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE submission_versions SET commit_sha = 'deadbeef'")
        conn.execute("DROP TRIGGER submission_versions_locked_update")
        conn.execute("UPDATE submission_versions SET commit_sha = 'deadbeef'")
        result = verify_chain(conn, wid)
    assert not result["ok"] and result["error"]["code"] == "VERSION_ALTERED"
    # and a reviewer can no longer file a report against a version that changed
    with pytest.raises(TransitionBlocked) as e:
        submit_report(app, wid, r1)
    assert e.value.code == "EVIDENCE_CHANGED"


def test_flipping_the_stored_mode_is_detected(setup):
    app, dev, r1, r2, r3, wid = setup
    to_validating(app, wid, dev, [r1])
    execute(app, "UPDATE workflow_instances SET review_mode = 'verification' WHERE id = ?", (wid,))
    result = replay(app, wid)
    assert not result["ok"] and result["error"]["code"] == "MODE_MISMATCH"


def test_deleting_or_adding_a_report_outside_the_chain_is_detected(setup):
    app, dev, r1, r2, r3, wid = setup
    to_validating(app, wid, dev, [r1, r2], required=2)
    submit_report(app, wid, r1)
    with get_db_connection_for(app) as conn:
        conn.execute("DROP TRIGGER review_reports_no_delete")
        conn.execute("DROP TRIGGER review_findings_no_delete")
        conn.execute("DELETE FROM review_reports")
        result = verify_chain(conn, wid)
    assert not result["ok"] and result["error"]["code"] in ("REPORT_MISMATCH",)


def test_altering_a_finding_is_detected(setup):
    app, dev, r1, r2, r3, wid = setup
    to_validating(app, wid, dev, [r1])
    submit_report(app, wid, r1)
    with get_db_connection_for(app) as conn:
        conn.execute("DROP TRIGGER review_findings_no_update")
        conn.execute("UPDATE review_findings SET severity = 'low'")
        result = verify_chain(conn, wid)
    assert not result["ok"] and result["error"]["code"] == "REPORT_MISMATCH"


def test_report_validation_errors_store_nothing(setup):
    app, dev, r1, r2, r3, wid = setup
    to_validating(app, wid, dev, [r1])
    bad = [report_extra(summary=""), report_extra(findings=[finding(severity="urgent")]),
           report_extra(findings=[finding(category="vibes")]), report_extra(findings=[finding(title="")]),
           report_extra(findings=[finding(commit_ref="zzz")]), report_extra(interview_qs="Why Flask?"),
           report_extra(findings=[finding(prior_finding_id=999)])]
    for extra in bad:
        with pytest.raises(TransitionBlocked) as e:
            fire(app, wid, E.SUBMIT_REPORT, r1, None, extra)
        assert e.value.code == "GUARD_FAILED"
    assert count(app, "review_reports") == 0 and state(app, wid) == "VALIDATING"


def test_interview_goal_accepts_interview_questions_and_categories(app):
    dev = make_user(app, "devfour"); r1 = make_user(app, "rev_q", "reviewer")
    _, wid = make_review_request(app, dev, goal="interview")
    to_validating(app, wid, dev, [r1])
    submit_report(app, wid, r1, interview_qs="Why SQLite?",
                  findings=[finding(category="explainability", severity="medium")])
    assert state(app, wid) == "REVIEWED"
