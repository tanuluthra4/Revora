"""Helpers for review-mode (Revora) tests."""
from automata.states import WorkflowEvent as E
from database.db import get_db_connection, transaction
from services.report_service import CONSENT_VERSION
from services.version_service import create_review_request
from tests.helpers import assign_many, fire, query

VERSION_FORM = {"review_goal": "production", "repo_url": "https://github.com/octocat/hello-world",
                "commit_sha": "7fd1a60b01f91b314f59955a4e4d4e80d8edf11d",
                "tech_stack": "Python, Flask, SQLite", "demo_url": "", "questions": "Is auth sound?"}


def make_project(app, owner_id, title="Demo project"):
    with app.app_context(), transaction() as conn:
        return conn.execute("INSERT INTO projects (user_id, title) VALUES (?, ?)", (owner_id, title)).lastrowid


def make_review_request(app, owner_id, goal="production", title="Review my API", project_id=None):
    project_id = project_id or make_project(app, owner_id)
    form = dict(VERSION_FORM, review_goal=goal)
    with app.app_context():
        conn = get_db_connection()
        try:
            return create_review_request(conn, owner_id, project_id, title, form)
        finally:
            conn.close()


def to_validating(app, workflow_id, owner_id, reviewer_ids, required=1):
    fire(app, workflow_id, E.START, owner_id)
    fire(app, workflow_id, E.REQUEST_EVIDENCE, owner_id)
    assign_many(app, workflow_id, owner_id, reviewer_ids, required)
    fire(app, workflow_id, E.SUBMIT_EVIDENCE, owner_id)
    fire(app, workflow_id, E.START_VALIDATION, reviewer_ids[0])


def finding(**over):
    base = {"severity": "high", "category": "security", "title": "Session cookie lacks Secure flag",
            "explanation": "Cookies are sent over plain HTTP in production.",
            "suggested_fix": "Set SESSION_COOKIE_SECURE when REVORA_ENV=production.",
            "reasoning": "Observed in config.py.", "file_path": "config.py", "commit_ref": "",
            "repro_evidence": ""}
    base.update(over)
    return base


def report_extra(consent=True, findings=None, version=CONSENT_VERSION, **report_over):
    report = {"summary": "A reasonable small Flask app with a few gaps.",
              "strengths": "Clear structure and tests.",
              "assessed": "Routing, auth and data access in the commit.",
              "not_assessed": "Deployment, load behaviour and the front end.", "unresolved": "", "interview_qs": ""}
    report.update(report_over)
    extra = {"report": report, "findings": [finding()] if findings is None else findings,
             "private_notes": "Private: author seems new to SQL."}
    if consent:
        extra["consent_ack"] = "1"
        extra["consent_version"] = version
    return extra


def submit_report(app, workflow_id, reviewer_id, **kw):
    return fire(app, workflow_id, E.SUBMIT_REPORT, reviewer_id, None, report_extra(**kw))


def count(app, table, where="1=1", params=()):
    return query(app, f"SELECT COUNT(*) FROM {table} WHERE {where}", params)[0][0]
