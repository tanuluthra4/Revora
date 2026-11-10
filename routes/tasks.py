import re
from collections import OrderedDict

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for

from automata.states import WorkflowEvent, WorkflowState
from automata.transitions import events_available_to, get_allowed_events
from database.db import get_db_connection, transaction
from routes.helpers import developer_required, flash_errors, login_required, reviewer_required
from services.automata_engine import AutomataEngine, TransitionBlocked, WorkflowNotFound
from services.evidence_service import (
    EVIDENCE_TYPES,
    EvidenceError,
    EvidenceNotAllowed,
    add_evidence,
    assign_reviewers,
    delete_draft_evidence,
    list_evidence,
    score_evidence,
)
from services.fsm_view import EVENT_LABELS, STATE_LABELS, build_fsm_view
from services.review_service import review_panel, tally
from services.share_service import create_link, revoke_link
from services.reviewer_profiles import ProfileError, selectable_reviewers

tasks_bp = Blueprint("tasks", __name__)
engine = AutomataEngine()

PRIORITIES = ("low", "medium", "high")
LOCKED_FOR_EDIT = {"EVIDENCE_SUBMITTED", "VALIDATING", "VERIFIED"}
# Reviewers may open a task only once its evidence has been submitted.
REVIEWER_VISIBLE = {"EVIDENCE_SUBMITTED", "VALIDATING", "VERIFIED", "REJECTED", "REVIEWED"}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


def _read_task_form(conn):
    """Validate the task form. Returns (values, problems)."""
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip() or None
    priority = request.form.get("priority", "medium")
    due_date = request.form.get("due_date", "").strip() or None
    project_id = request.form.get("project_id", "").strip() or None
    problems = []

    if not title:
        problems.append("Task title is required.")
    elif len(title) > 200:
        problems.append("Task title must be at most 200 characters.")
    if description and len(description) > 2000:
        problems.append("Description must be at most 2000 characters.")
    if priority not in PRIORITIES:
        problems.append("Choose a valid priority.")
    if due_date and not _DATE.match(due_date):
        problems.append("Due date must look like YYYY-MM-DD.")

    if project_id is not None:
        owned = (
            conn.execute(
                "SELECT id FROM projects WHERE id = ? AND user_id = ?",
                (project_id, g.user["id"]),
            ).fetchone()
            if project_id.isdigit() else None
        )
        if owned is None:
            problems.append("Choose one of your own projects.")
        else:
            project_id = owned["id"]

    values = {
        "title": title, "description": description, "priority": priority,
        "due_date": due_date, "project_id": project_id,
    }
    return values, problems


def _redirect_after_event():
    if request.form.get("next") == "dashboard":
        return url_for("main.dashboard")
    return None


# -------------------------------------------------------------- create/edit
@tasks_bp.route("/add-task", methods=["GET", "POST"])
@developer_required
def add_task():
    conn = get_db_connection()
    try:
        projects = _user_projects(conn)
        if request.method == "POST":
            values, problems = _read_task_form(conn)
            if problems:
                flash_errors(problems)
                return render_template("task_form.html", projects=projects, task=values,
                                       mode="create", priorities=PRIORITIES)

            # The task and its workflow are created together or not at all.
            with transaction(conn):
                cursor = conn.execute(
                    """
                    INSERT INTO tasks (user_id, project_id, title, description, status,
                                       priority, due_date)
                    VALUES (?, ?, ?, ?, 'Pending', ?, ?)
                    """,
                    (g.user["id"], values["project_id"], values["title"],
                     values["description"], values["priority"], values["due_date"]),
                )
                task_id = cursor.lastrowid
                engine.create_workflow(g.user["id"], task_id, conn=conn)

            flash("Task created. Its proof workflow starts in CREATED.", "success")
            return redirect(url_for("tasks.task_detail", task_id=task_id))

        return render_template("task_form.html", projects=projects, task={}, mode="create",
                               priorities=PRIORITIES)
    finally:
        conn.close()


@tasks_bp.route("/tasks/<int:task_id>/edit", methods=["GET", "POST"])
@developer_required
def edit_task(task_id):
    conn = get_db_connection()
    try:
        task = _owned_task_or_404(conn, task_id)
        if task["current_state"] in LOCKED_FOR_EDIT:
            flash("This task is locked while its evidence is under review or after verification.",
                  "error")
            return redirect(url_for("tasks.task_detail", task_id=task_id))

        projects = _user_projects(conn)
        if request.method == "POST":
            values, problems = _read_task_form(conn)
            if problems:
                flash_errors(problems)
                values["id"] = task_id
                return render_template("task_form.html", projects=projects, task=values,
                                       mode="edit", priorities=PRIORITIES)
            with transaction(conn):
                conn.execute(
                    """
                    UPDATE tasks SET title = ?, description = ?, project_id = ?,
                                     priority = ?, due_date = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (values["title"], values["description"], values["project_id"],
                     values["priority"], values["due_date"], task_id, g.user["id"]),
                )
            flash("Task updated.", "success")
            return redirect(url_for("tasks.task_detail", task_id=task_id))

        return render_template("task_form.html", projects=projects, task=dict(task), mode="edit",
                               priorities=PRIORITIES)
    finally:
        conn.close()


@tasks_bp.route("/tasks/<int:task_id>/delete", methods=["POST"])
@developer_required
def delete_task(task_id):
    conn = get_db_connection()
    try:
        task = _owned_task_or_404(conn, task_id)
        if task["review_mode"] == "review" and conn.execute(
                "SELECT 1 FROM review_reports WHERE workflow_id = ?", (task["workflow_id"],)).fetchone():
            flash("A request that has reviewer reports is a permanent record and cannot be deleted.", "error")
            return redirect(url_for("tasks.task_detail", task_id=task_id))
        if task["current_state"] == "VERIFIED":
            flash("Verified tasks are permanent proof records and cannot be deleted.", "error")
            return redirect(url_for("tasks.task_detail", task_id=task_id))
        with transaction(conn):
            conn.execute("DELETE FROM tasks WHERE id = ? AND user_id = ?", (task_id, g.user["id"]))
        flash("Task deleted.", "success")
    finally:
        conn.close()
    return redirect(url_for("main.dashboard"))


@tasks_bp.route("/tasks/<int:task_id>/complete", methods=["POST"])
@developer_required
def complete_task(task_id):
    """Self-reported completion. It does NOT verify anything."""
    conn = get_db_connection()
    try:
        _owned_task_or_404(conn, task_id)
        with transaction(conn):
            conn.execute(
                "UPDATE tasks SET status = 'Completed', "
                "completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP) "
                "WHERE id = ? AND user_id = ?",
                (task_id, g.user["id"]),
            )
        flash("Marked done (self-reported). Submit evidence to get it verified.", "success")
    finally:
        conn.close()
    return redirect(_redirect_after_event() or url_for("tasks.task_detail", task_id=task_id))


# ------------------------------------------------------------------- detail
@tasks_bp.route("/tasks/<int:task_id>")
@login_required
def task_detail(task_id):
    conn = get_db_connection()
    try:
        task = _load_task(conn, task_id)
        access = _access_for(conn, task)
        if access is None:
            abort(404)
        if task["review_mode"] == "review":
            return redirect(url_for("reviews.detail", task_id=task_id))

        is_owner = access == "owner"
        is_reviewer = access == "reviewer"
        state = WorkflowState(task["current_state"])
        workflow_id = task["workflow_id"]
        closed = task["current_state"] in ("VERIFIED", "REJECTED")

        panel = review_panel(conn, workflow_id, task["round"], g.user["id"], is_owner,
                             task["current_state"])
        active = [r for r in panel if r["status"] == "assigned"]
        me = next((r for r in panel if r["is_me"]), None)
        my_decided = bool(me and me["decided"])
        decisions_visible = is_owner or closed or my_decided
        approvals, rejections, total = tally(panel)
        decided_count = sum(1 for r in active if r["decided"])

        history = engine.get_history(workflow_id)
        if is_reviewer and not decisions_visible:
            # Independence: other reviewers' decisions stay hidden until you have decided.
            history = [
                {**dict(h), "event": "VOTE (hidden until you decide)", "reason": None}
                if h["event"] in ("VOTE_APPROVE", "VOTE_REJECT") else h
                for h in history
            ]
        actions = events_available_to(state, is_owner, is_reviewer)
        if my_decided:
            actions = [e for e in actions if e not in (WorkflowEvent.VOTE_APPROVE,
                                                       WorkflowEvent.VOTE_REJECT)]
        # Owner-only maintenance actions are offered through dedicated forms, not generic buttons.
        actions = [e for e in actions if e not in (WorkflowEvent.REASSIGN_REVIEWER,
                                                   WorkflowEvent.WITHDRAW)]
        fsm = build_fsm_view(task["current_state"], history, actions)

        evidence_by_round = OrderedDict()
        for item in list_evidence(conn, workflow_id):
            if is_reviewer and item["round"] != task["round"]:
                continue        # reviewers see only the evidence version assigned to them
            evidence_by_round.setdefault(item["round"], []).append(item)
        current_round_items = evidence_by_round.get(task["round"], [])
        # While REJECTED the round counter has not advanced yet, so the rejected
        # submission is still "current"; after RESTART/WITHDRAW a new round starts.
        scored_items = current_round_items or (
            next(iter(evidence_by_round.values()), []) if evidence_by_round else []
        )

        probe_events = [e for e in WorkflowEvent if e not in get_allowed_events(state)]
        in_review = task["current_state"] in ("EVIDENCE_SUBMITTED", "VALIDATING")
        share_active = bool(task["share_token"])

        return render_template(
            "task_detail.html",
            task=task,
            is_owner=is_owner,
            is_reviewer=is_reviewer,
            state=state,
            state_label=STATE_LABELS[state],
            actions=actions,
            event_labels=EVENT_LABELS,
            probe_events=probe_events,
            fsm=fsm,
            history=history,
            blocked=engine.get_blocked_actions(workflow_id) if is_owner else [],
            chain=engine.verify_history(workflow_id),
            evidence_by_round=evidence_by_round,
            current_round=task["round"],
            evidence_score=score_evidence(scored_items),
            evidence_types=EVIDENCE_TYPES,
            panel=panel,
            active_reviewers=active,
            my_decided=my_decided,
            decisions_visible=decisions_visible,
            approvals=approvals, rejections=rejections, total=total,
            decided_count=decided_count,
            can_add_evidence=is_owner and state == WorkflowState.EVIDENCE_REQUIRED,
            can_edit=is_owner and task["current_state"] not in LOCKED_FOR_EDIT,
            can_assign=is_owner and (state == WorkflowState.EVIDENCE_REQUIRED or (
                state == WorkflowState.EVIDENCE_SUBMITTED and not active)),
            can_reassign=is_owner and in_review and any(not r["decided"] for r in active),
            can_withdraw=is_owner and in_review,
            default_required=current_app.config["DEFAULT_REQUIRED_APPROVALS"],
            share_url=(url_for("public.proof", token=task["share_token"], _external=True)
                       if share_active else None),
            share_revoked=not share_active and bool(task["share_revoked_count"]),
        )
    finally:
        conn.close()


# ------------------------------------------------------- workflow events
@tasks_bp.route("/tasks/<int:task_id>/events/<event_name>", methods=["POST"])
@login_required
def fire_event(task_id, event_name):
    conn = get_db_connection()
    try:
        task = _load_task(conn, task_id)
        if _access_for(conn, task) is None:
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


# ---------------------------------------------------------------- evidence
@tasks_bp.route("/tasks/<int:task_id>/evidence", methods=["POST"])
@developer_required
def submit_evidence_item(task_id):
    conn = get_db_connection()
    try:
        task = _owned_task_or_404(conn, task_id)
        try:
            add_evidence(
                conn,
                task["workflow_id"],
                g.user["id"],
                request.form.get("evidence_type"),
                request.form.get("evidence_url"),
                request.form.get("title"),
                request.form.get("description"),
            )
            flash("Evidence added as a draft. Submit it for review when you are ready.", "success")
        except EvidenceError as error:
            flash_errors(error.problems)
        except EvidenceNotAllowed as error:
            flash(str(error), "error")
    finally:
        conn.close()
    return redirect(url_for("tasks.task_detail", task_id=task_id))


@tasks_bp.route("/tasks/<int:task_id>/evidence/<int:evidence_id>/delete", methods=["POST"])
@developer_required
def remove_evidence_item(task_id, evidence_id):
    conn = get_db_connection()
    try:
        task = _owned_task_or_404(conn, task_id)
        try:
            delete_draft_evidence(conn, task["workflow_id"], g.user["id"], evidence_id)
            flash("Draft evidence removed.", "success")
        except EvidenceNotAllowed as error:
            flash(str(error), "error")
    finally:
        conn.close()
    return redirect(url_for("tasks.task_detail", task_id=task_id))


# ------------------------------------------------- reviewer + sharing
@tasks_bp.route("/tasks/<int:task_id>/reviewer", methods=["POST"])
@developer_required
def choose_reviewer(task_id):
    """Save the chosen reviewers. Ids come from the directory picker and are re-validated
    against the database here: real reviewer accounts, listed, distinct, never the owner."""
    conn = get_db_connection()
    try:
        task = _owned_task_or_404(conn, task_id)
        try:
            chosen = selectable_reviewers(conn, request.form.getlist("reviewer_ids"), g.user["id"])
            names, required = assign_reviewers(
                conn, task["workflow_id"], g.user["id"],
                [username for _, username, _ in chosen], request.form.get("required"),
            )
            session.pop(f"pick:{task_id}", None)
            if task["review_mode"] == "review":
                flash(f"This version will be sent to {', '.join(names)}; {required} of {len(names)} "
                      "must submit a report. Selecting reviewers does not guarantee any particular outcome.",
                      "success")
            else:
                flash(f"Evidence will be sent to {', '.join(names)}; "
                      f"{required} of {len(names)} must approve. Selecting reviewers does not guarantee approval.",
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


@tasks_bp.route("/tasks/<int:task_id>/share", methods=["POST"])
@developer_required
def share_task(task_id):
    """Create a public, read-only link to a VERIFIED proof (private until you do this)."""
    conn = get_db_connection()
    try:
        task = _owned_task_or_404(conn, task_id)
        if task["current_state"] != "VERIFIED":
            flash("Only verified proofs can be shared.", "error")
        else:
            create_link(conn, task["workflow_id"], task["round"], "proof", g.user["id"])
            flash("Public link created. Anyone with the link can view this verified proof.", "success")
    finally:
        conn.close()
    return redirect(url_for("tasks.task_detail", task_id=task_id))


@tasks_bp.route("/tasks/<int:task_id>/unshare", methods=["POST"])
@developer_required
def unshare_task(task_id):
    """Revoke the public link: it stops exposing the proof and says it was revoked."""
    conn = get_db_connection()
    try:
        task = _owned_task_or_404(conn, task_id)
        revoke_link(conn, task["workflow_id"], task["round"], "proof")
        flash("Public link revoked. It no longer shows this proof.", "success")
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
            WHERE w.user_id != ? AND w.current_state IN (%s)
            ORDER BY last_change %s
        """
        me = g.user["id"]
        queue = conn.execute(base % ("'EVIDENCE_SUBMITTED', 'VALIDATING'", "ASC"), (me, me, me, me)).fetchall()
        decided = conn.execute(base % ("'VERIFIED', 'REJECTED', 'REVIEWED'", "DESC"), (me, me, me, me)).fetchall()
    finally:
        conn.close()
    return render_template("review_queue.html", queue=queue, decided=decided)
