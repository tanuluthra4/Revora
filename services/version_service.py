"""Review requests and their versions.

A review request is a task + workflow in review mode; each round of that
workflow is one *version* (`submission_versions`). Revora never fetches a
repository, never runs submitted code and never asks for repository
credentials: it records a URL and a commit SHA that the owner typed, and the
reviewers look at them on their own.
"""
import re

from database.db import transaction
from services.evidence_service import EvidenceError, EvidenceNotAllowed, _valid_http_url
from services.report_service import GOALS

_SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")
_SECRET = re.compile(r"(ghp_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{10,}|glpat-[A-Za-z0-9_\-]{10,}"
                     r"|gho_[A-Za-z0-9]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)")
EDITABLE_STATES = ("CREATED", "IN_PROGRESS", "EVIDENCE_REQUIRED")
GOAL_LABELS = {"production": "Production readiness", "interview": "Interview preparation"}


def validate_version(form):
    """Clean the version fields. Raises EvidenceError listing every problem."""
    problems = []
    goal = (form.get("review_goal") or "").strip()
    if goal not in GOALS:
        problems.append("Choose a review goal.")
    repo = (form.get("repo_url") or "").strip()
    if not repo or not _valid_http_url(repo) or len(repo) > 300:
        problems.append("Enter the repository address as a full http(s) link (no username or password in it).")
    sha = (form.get("commit_sha") or "").strip()
    if not _SHA.match(sha):
        problems.append("Enter the commit SHA being reviewed (7-40 hexadecimal characters).")
    demo = (form.get("demo_url") or "").strip() or None
    if demo and (not _valid_http_url(demo) or len(demo) > 300):
        problems.append("The demo address must be a full http(s) link.")
    stack = (form.get("tech_stack") or "").strip()
    if not stack:
        problems.append("Describe the tech stack.")
    if len(stack) > 300:
        problems.append("Tech stack can be at most 300 characters.")
    questions = (form.get("questions") or "").strip() or None
    if questions and len(questions) > 2000:
        problems.append("Questions can be at most 2000 characters.")
    for text in (repo, demo or "", stack, questions or ""):
        if _SECRET.search(text):
            problems.append("That looks like a secret (token or key). Revora never needs one: "
                            "remove it, and revoke it if it was real.")
            break
    if problems:
        raise EvidenceError(problems)
    return {"review_goal": goal, "repo_url": repo, "commit_sha": sha.lower(), "demo_url": demo,
            "tech_stack": stack, "questions": questions}


def create_review_request(conn, owner_id, project_id, title, form):
    """Create the task, its review-mode workflow and version 1. Returns (task_id, workflow_id).

    A project is required: a review is always about a named project of the owner.
    """
    title = (title or "").strip()
    if not title or len(title) > 200:
        raise EvidenceError(["Give the request a title (at most 200 characters)."])
    fields = validate_version(form)
    with transaction(conn):
        project = conn.execute("SELECT id FROM projects WHERE id = ? AND user_id = ?",
                               (project_id, owner_id)).fetchone() if str(project_id or "").isdigit() else None
        if project is None:
            raise EvidenceError(["Choose one of your own projects."])
        task_id = conn.execute(
            "INSERT INTO tasks (user_id, project_id, title, status) VALUES (?, ?, ?, 'Pending')",
            (owner_id, project["id"], title)).lastrowid
        workflow_id = conn.execute(
            "INSERT INTO workflow_instances (user_id, task_id, current_state, review_mode) "
            "VALUES (?, ?, 'CREATED', 'review')", (owner_id, task_id)).lastrowid
        conn.execute(
            "INSERT INTO submission_versions (workflow_id, round, project_id, review_goal, repo_url, "
            "commit_sha, demo_url, tech_stack, questions) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)",
            (workflow_id, project["id"], fields["review_goal"], fields["repo_url"], fields["commit_sha"],
             fields["demo_url"], fields["tech_stack"], fields["questions"]))
    return task_id, workflow_id


def get_version(conn, workflow_id, round_no):
    return conn.execute("SELECT * FROM submission_versions WHERE workflow_id = ? AND round = ?",
                        (workflow_id, round_no)).fetchone()


def update_version(conn, workflow_id, owner_id, form):
    """Edit the current version while it is still a draft (never after submission)."""
    fields = validate_version(form)
    with transaction(conn):
        wf = conn.execute("SELECT user_id, current_state, round, review_mode FROM workflow_instances "
                          "WHERE id = ?", (workflow_id,)).fetchone()
        if wf is None or wf["user_id"] != owner_id or wf["review_mode"] != "review":
            raise EvidenceNotAllowed("Request not found.")
        if wf["current_state"] not in EDITABLE_STATES:
            raise EvidenceNotAllowed("A version that has been submitted cannot be edited. "
                                     "Withdraw it, or start a new version after the review.")
        cur = conn.execute(
            "UPDATE submission_versions SET review_goal = ?, repo_url = ?, commit_sha = ?, demo_url = ?, "
            "tech_stack = ?, questions = ? WHERE workflow_id = ? AND round = ? AND locked_at IS NULL",
            (fields["review_goal"], fields["repo_url"], fields["commit_sha"], fields["demo_url"],
             fields["tech_stack"], fields["questions"], workflow_id, wf["round"]))
        if cur.rowcount != 1:
            raise EvidenceNotAllowed("This version is locked.")
