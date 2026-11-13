from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for

from automata.states import WorkflowEvent
from database.db import get_db_connection, transaction
from routes.helpers import developer_required, flash_errors, login_required, reviewer_required
from services.automata_engine import AutomataEngine, TransitionBlocked, WorkflowNotFound
from services.evidence_service import EvidenceError, EvidenceNotAllowed, assign_reviewers
from services.fsm_view import EVENT_LABELS
from services.reviewer_profiles import ProfileError, selectable_reviewers

tasks_bp = Blueprint("tasks", __name__)
engine = AutomataEngine()

# Reviewers may open a task only once its evidence has been submitted.
REVIEWER_VISIBLE = {"EVIDENCE_SUBMITTED", "VALIDATING", "REVIEWED"}


# ------------------------------------------------------------------ helpers
def _load_task(conn, task_id):
    return conn.execute(
        """
        SELECT t.id, t.user_id, t.project_id, t.title, t.description, t.status, t.priority,
               t.due_date, t.created_at, p.title AS project_title,
               w.id AS workflow_id, w.current_state, w.round, w.result,
               w.completed_at AS decided_at, w.review_mode,
               (SELECT token FROM share_links s WHERE s.workflow_id = w.id AND s.round = w.round
                  AND s.kind = 'proof' AND s.revoked_at IS NULL) AS share_token,
               (SELECT COUNT(*) FROM share_links s WHERE s.workflow_id = w.id AND s.round = w.round
                  AND s.kind = 'proof' AND s.revoked_at IS NOT NULL) AS share_revoked_count,
               w.required_approvals, u.username AS owner_name
        FROM tasks t
        JOIN workflow_instances w ON w.task_id = t.id
        JOIN users u ON u.id = t.user_id
        LEFT JOIN projects p ON p.id = t.project_id
        WHERE t.id = ?
        """,
        (task_id,),
    ).fetchone()


def _access_for(conn, task):
    """'owner', 'reviewer' or None. None must look like a 404 to the caller."""
    if task is None:
        return None
    if task["user_id"] == g.user["id"]:
        return "owner"
    # A reviewer only ever sees evidence that was assigned to *them* for the current round.
    if g.user["role"] == "reviewer" and task["current_state"] in REVIEWER_VISIBLE:
        assigned = conn.execute(
            "SELECT 1 FROM review_assignments WHERE workflow_id = ? AND round = ? "
            "AND reviewer_id = ? AND status = 'assigned'",
            (task["workflow_id"], task["round"], g.user["id"]),
        ).fetchone()
        if assigned:
            return "reviewer"
    return None


def _owned_task_or_404(conn, task_id):
    task = _load_task(conn, task_id)
    if task is None or task["user_id"] != g.user["id"]:
        abort(404)
    return task


def _user_projects(conn):
    return conn.execute(
        "SELECT id, title FROM projects WHERE user_id = ? ORDER BY created_at DESC",
        (g.user["id"],),
    ).fetchall()


def _redirect_after_event():
    if request.form.get("next") == "dashboard":
        return url_for("main.dashboard")
    return None


# ------------------------------------------------------------------ delete
@tasks_bp.route("/tasks/<int:task_id>/delete", methods=["POST"])
@developer_required
def delete_task(task_id):
    conn = get_db_connection()
    try:
        task = _owned_task_or_404(conn, task_id)
        if conn.execute("SELECT 1 FROM review_reports WHERE workflow_id = ?", (task["workflow_id"],)).fetchone():
            flash("A request that has reviewer reports is a permanent record and cannot be deleted.", "error")
            return redirect(url_for("reviews.detail", task_id=task_id))
        with transaction(conn):
            conn.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, g.user["id"]))
        flash("Request deleted.", "success")
    finally:
        conn.close()
    return redirect(url_for("main.dashboard"))


# ------------------------------------------------------------------- detail
@tasks_bp.route("/tasks/<int:task_id>")
@login_required
def task_detail(task_id):
    """Kept so older links keep working: every request now lives on the review page."""
    conn = get_db_connection()
    try:
        task = _load_task(conn, task_id)
        if _access_for(conn, task) is None or task["review_mode"] != "review":
            abort(404)
    finally:
        conn.close()
    return redirect(url_for("reviews.detail", task_id=task_id))


# ------------------------------------------------------- workflow events
@tasks_bp.route("/tasks/<int:task_id>/events/<event_name>", methods=["POST"])
@login_required
def fire_event(task_id, event_name):
    conn = get_db_connection()
    try:
        task = _load_task(conn, task_id)
        if _access_for(conn, task) is None or task["review_mode"] != "review":
            abort(404)
    finally:
        conn.close()

    extra = {
        "private_notes": request.form.get("private_notes"),
        "note_public": request.form.get("note_public") == "1",
        "remove": request.form.get("remove"),
        "add": request.form.get("add"),
    }
    try:
        result = engine.transition(
            task["workflow_id"], event_name, g.user["id"], request.form.get("reason"), extra
        )
        label = EVENT_LABELS.get(WorkflowEvent(result["event"]), result["event"])
        flash(f"{label}: {result['from_state']} → {result['to_state']}.", "success")
    except TransitionBlocked as blocked:
        flash(f"Blocked ({blocked.code}): {blocked}", "error")
    except WorkflowNotFound:
        abort(404)

    return redirect(_redirect_after_event() or url_for("tasks.task_detail", task_id=task_id))


# ------------------------------------------------- reviewer + sharing
@tasks_bp.route("/tasks/<int:task_id>/reviewer", methods=["POST"])
@developer_required
def choose_reviewer(task_id):
    """Save the chosen reviewers. Ids come from the directory picker and are re-validated
    against the database here: real reviewer accounts, listed, distinct, never the owner."""
    conn = get_db_connection()
    try:
        task = _owned_task_or_404(conn, task_id)
        if task["review_mode"] != "review":
            abort(404)
        try:
            chosen = selectable_reviewers(conn, request.form.getlist("reviewer_ids"), g.user["id"])
            names, required = assign_reviewers(
                conn, task["workflow_id"], g.user["id"],
                [username for _, username, _ in chosen], request.form.get("required"),
            )
            session.pop(f"pick:{task_id}", None)
            flash(f"This version will be sent to {', '.join(names)}; {required} of {len(names)} "
                  "must submit a report. Selecting reviewers does not guarantee any particular outcome.",
                  "success")
        except ProfileError as error:
            flash_errors(error.problems)
            return redirect(url_for("reviewers.picker", task_id=task_id))
        except EvidenceError as error:
            flash_errors(error.problems)
            return redirect(url_for("reviewers.picker", task_id=task_id))
        except EvidenceNotAllowed as error:
            flash(str(error), "error")
    finally:
        conn.close()
    return redirect(url_for("tasks.task_detail", task_id=task_id))


# ------------------------------------------------------------ review queue
@tasks_bp.route("/review")
@reviewer_required
def review_queue():
    conn = get_db_connection()
    try:
        base = """
            SELECT t.id AS task_id, t.title, u.username AS owner_name, w.current_state,
                   w.round, w.required_approvals, w.completed_at AS decided_at,
                   (SELECT COUNT(*) FROM evidence e
                     WHERE e.workflow_id = w.id AND e.round = w.round) AS evidence_count,
                   (SELECT COUNT(*) FROM review_assignments x
                     WHERE x.workflow_id = w.id AND x.round = w.round
                       AND x.status = 'assigned') AS reviewer_count,
                   (SELECT COUNT(*) FROM review_decisions d
                     WHERE d.workflow_id = w.id AND d.round = w.round)
                   + (SELECT COUNT(*) FROM review_reports q
                     WHERE q.workflow_id = w.id AND q.round = w.round) AS decided_count,
                   (SELECT COUNT(*) FROM review_decisions m
                     WHERE m.workflow_id = w.id AND m.round = w.round
                       AND m.reviewer_id = ?)
                   + (SELECT COUNT(*) FROM review_reports mq
                     WHERE mq.workflow_id = w.id AND mq.round = w.round
                       AND mq.reviewer_id = ?) AS my_decisions, w.review_mode,
                   (SELECT MAX(created_at) FROM state_transitions s
                     WHERE s.workflow_id = w.id) AS last_change
            FROM workflow_instances w
            JOIN review_assignments a ON a.workflow_id = w.id AND a.round = w.round
                                     AND a.reviewer_id = ? AND a.status = 'assigned'
            JOIN tasks t ON t.id = w.task_id
            JOIN users u ON u.id = w.user_id
            WHERE w.user_id != ? AND w.review_mode = 'review' AND w.current_state IN (%s)
            ORDER BY last_change %s
        """
        me = g.user["id"]
        queue = conn.execute(base % ("'EVIDENCE_SUBMITTED', 'VALIDATING'", "ASC"), (me, me, me, me)).fetchall()
        decided = conn.execute(base % ("'REVIEWED'", "DESC"), (me, me, me, me)).fetchall()
    finally:
        conn.close()
    return render_template("review_queue.html", queue=queue, decided=decided)
