from flask import Blueprint, g, redirect, render_template, url_for

from automata.states import WorkflowState
from automata.transitions import events_available_to
from database.db import get_db_connection
from routes.helpers import developer_required
from services.fsm_view import EVENT_LABELS, STATE_LABELS, progress_percent

main_bp = Blueprint("main", __name__)

NEEDS_ACTION = {"CREATED", "IN_PROGRESS", "EVIDENCE_REQUIRED"}
IN_REVIEW = {"EVIDENCE_SUBMITTED", "VALIDATING"}


@main_bp.route("/")
def home():
    if g.get("user"):
        if g.user["role"] == "reviewer":
            return redirect(url_for("tasks.review_queue"))
        return redirect(url_for("main.dashboard"))
    return render_template("welcome.html")


@main_bp.route("/healthz")
def healthz():
    return {"status": "ok"}


@main_bp.route("/dashboard")
@developer_required
def dashboard():
    conn = get_db_connection()
    try:
        tasks = conn.execute(
            """
            SELECT t.id, t.title, t.status, t.priority, t.due_date, p.title AS project_title,
                   w.current_state AS workflow_state, w.round AS workflow_round, w.review_mode
            FROM tasks t
            LEFT JOIN workflow_instances w ON w.task_id = t.id
            LEFT JOIN projects p ON p.id = t.project_id
            WHERE t.user_id = ? AND w.review_mode = 'review'
            ORDER BY t.id DESC
            """,
            (g.user["id"],),
        ).fetchall()
    finally:
        conn.close()

    cards = []
    counts = {"in_review": 0, "needs_action": 0, "reviewed": 0}
    for task in tasks:
        state_value = task["workflow_state"] or "CREATED"
        state = WorkflowState(state_value)
        if state_value == "REVIEWED":
            counts["reviewed"] += 1
        elif state_value in IN_REVIEW:
            counts["in_review"] += 1
        elif state_value in NEEDS_ACTION:
            counts["needs_action"] += 1

        cards.append({
            "task": task,
            "state": state_value,
            "state_label": STATE_LABELS[state],
            "progress": progress_percent(state_value),
            "actions": events_available_to(state, is_owner=True, is_reviewer=False),
        })

    return render_template(
        "dashboard.html",
        cards=cards,
        counts=counts,
        total=len(cards),
        event_labels=EVENT_LABELS,
    )
