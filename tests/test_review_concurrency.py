"""Concurrent requests against the review workflow must not corrupt state or history."""
import threading

import pytest

from automata.states import WorkflowEvent as E
from database.db import get_db_connection
from services.automata_engine import TransitionBlocked
from services.finding_service import FindingError, FindingNotAllowed, update_finding
from services.integrity import verify_chain
from tests.helpers import make_user, query
from tests.review_helpers import count, make_review_request, report_extra, to_validating
from tests.helpers import fire


def run_together(fns):
    barrier = threading.Barrier(len(fns))
    results = [None] * len(fns)

    def wrap(i, fn):
        barrier.wait()
        try:
            results[i] = ("ok", fn())
        except Exception as exc:                       # noqa: BLE001 - we classify below
            results[i] = ("err", exc)

    threads = [threading.Thread(target=wrap, args=(i, f)) for i, f in enumerate(fns)]
    [t.start() for t in threads]; [t.join() for t in threads]
    return results


@pytest.fixture
def setup(app):
    dev = make_user(app, "devconc")
    r1, r2 = make_user(app, "rev_c1", "reviewer"), make_user(app, "rev_c2", "reviewer")
    _, wid = make_review_request(app, dev)
    return app, dev, r1, r2, wid


def test_same_reviewer_submitting_twice_at_once_stores_exactly_one_report(setup):
    app, dev, r1, r2, wid = setup
    to_validating(app, wid, dev, [r1, r2], required=2)
    results = run_together([lambda: fire(app, wid, E.SUBMIT_REPORT, r1, None, report_extra())
                            for _ in range(4)])
    assert [r[0] for r in results].count("ok") == 1
    assert all(isinstance(r[1], TransitionBlocked) for r in results if r[0] == "err")
    assert count(app, "review_reports") == 1
    with app.app_context():
        conn = get_db_connection()
        assert verify_chain(conn, wid)["ok"]
        conn.close()


def test_two_reviewers_reporting_at_once_complete_the_review_exactly_once(setup):
    app, dev, r1, r2, wid = setup
    to_validating(app, wid, dev, [r1, r2], required=2)
    results = run_together([lambda: fire(app, wid, E.SUBMIT_REPORT, r1, None, report_extra()),
                            lambda: fire(app, wid, E.SUBMIT_REPORT, r2, None, report_extra())])
    assert [r[0] for r in results] == ["ok", "ok"]
    assert query(app, "SELECT current_state FROM workflow_instances")[0][0] == "REVIEWED"
    assert query(app, "SELECT COUNT(*) FROM state_transitions WHERE event = 'COMPLETE_REVIEW'")[0][0] == 1
    assert count(app, "review_reports") == 2
    with app.app_context():
        conn = get_db_connection()
        assert verify_chain(conn, wid)["ok"]
        conn.close()


def test_concurrent_finding_updates_keep_one_valid_history(setup):
    app, dev, r1, r2, wid = setup
    to_validating(app, wid, dev, [r1])
    fire(app, wid, E.SUBMIT_REPORT, r1, None, report_extra())

    def owner_moves(status):
        def go():
            with app.app_context():
                conn = get_db_connection()
                try:
                    return update_finding(conn, 1, dev, status, "Working on it now.")
                finally:
                    conn.close()
        return go

    results = run_together([owner_moves("acknowledged"), owner_moves("acknowledged"),
                            owner_moves("fixed"), owner_moves("fixed")])
    ok = [r for r in results if r[0] == "ok"]
    assert ok and all(isinstance(r[1], (FindingError, FindingNotAllowed)) for r in results if r[0] == "err")
    seqs = [r[0] for r in query(app, "SELECT seq FROM finding_updates ORDER BY seq")]
    assert seqs == list(range(1, len(seqs) + 1))                          # no gaps, no duplicates
    with app.app_context():
        conn = get_db_connection()
        assert verify_chain(conn, wid)["ok"]                              # per-finding hash chain intact
        conn.close()
