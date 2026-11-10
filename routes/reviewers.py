"""Reviewer profiles, the reviewer directory, and the reviewer picker used when
submitting evidence. Authorisation is enforced here on every route."""
from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template, request,
                   session, url_for)

from automata.states import WorkflowState
from database.db import get_db_connection, transaction
from routes.helpers import developer_required, flash_errors, login_required, reviewer_required
from routes.tasks import _owned_task_or_404
from services import reviewer_profiles as rp

reviewers_bp = Blueprint("reviewers", __name__)

MAX_SELECTED = 5


@reviewers_bp.app_context_processor
def _pager():
    def pager_url(target_page):
        args = {k: v for k, v in request.args.items() if k in ("q", "skill", "expertise", "level", "required") and v}
        if target_page > 1:
            args["page"] = target_page
        return url_for(request.endpoint, **(request.view_args or {}), **args)
    return {"pager_url": pager_url}
FILTER_KEYS = ("q", "skill", "expertise", "level", "page")


def _filters(source):
    page = source.get("page", "1")
    return {
        "q": (source.get("q") or "").strip()[:60],
        "skill": (source.get("skill") or "").strip()[:30],
        "expertise": (source.get("expertise") or "").strip()[:30],
        "level": (source.get("level") or "").strip() if (source.get("level") or "") in rp.LEVELS else "",
        "page": int(page) if str(page).isdigit() else 1,
    }


# ------------------------------------------------------------- directory
@reviewers_bp.route("/reviewers")
@login_required
def directory():
    filters = _filters(request.args)
    conn = get_db_connection()
    try:
        items, total, page, pages = rp.directory(conn, **filters)
    finally:
        conn.close()
    filters["page"] = page
    return render_template("reviewer_directory.html", items=items, total=total, page=page, pages=pages,
                           filters=filters, levels=rp.LEVELS)


@reviewers_bp.route("/reviewers/<username>")
def public_profile(username):
    conn = get_db_connection()
    try:
        profile = rp.get_public(conn, username)
    finally:
        conn.close()
    if profile is None:
        abort(404)       # private, unknown and non-reviewer accounts look identical
    return render_template("reviewer_profile.html", profile=profile, levels=rp.LEVELS, preview=False)


# --------------------------------------------------------- own profile
@reviewers_bp.route("/reviewer/profile", methods=["GET", "POST"])
@reviewer_required
def edit_profile():
    conn = get_db_connection()
    try:
        form = request.form
        if request.method == "POST":
            # Only profile fields are read. users.role and every other account column are never touched.
            try:
                clean, skills, expertise = rp.validate_form(form)
            except rp.ProfileError as error:
                flash_errors(error.problems)
            else:
                with transaction(conn):
                    rp.save_profile(conn, g.user["id"], clean, skills, expertise)
                flash("Profile saved.", "success")
                return redirect(url_for("reviewers.edit_profile"))
            profile = rp.get_own(conn, g.user["id"])
            return render_template("reviewer_profile_edit.html", profile=profile, form=form, levels=rp.LEVELS,
                                   visibilities=rp.VISIBILITIES, stats=rp.review_stats(conn, g.user["id"]))
        profile = rp.get_own(conn, g.user["id"])
        if profile is None:
            abort(404)
        stats = rp.review_stats(conn, g.user["id"])
    finally:
        conn.close()
    r = profile["row"]
    values = {
        "display_name": r["full_name"] or "", "headline": r["headline"] or "", "bio": r["bio"] or "",
        "work_history": r["work_history"] or "", "education": r["education"] or "",
        "github_url": r["github_url"] or "", "portfolio_url": r["portfolio_url"] or "",
        "linkedin_url": r["linkedin_url"] or "", "experience_level": r["experience_level"] or "",
        "profile_visibility": r["profile_visibility"] if r["profile_visibility"] in rp.VISIBILITIES else "private",
        "skills": ", ".join(profile["skills"]), "expertise": ", ".join(profile["expertise"]),
        **{f: str(r[f]) for f in ("pub_bio", "pub_experience", "pub_work", "pub_education", "pub_links")},
    }
    return render_template("reviewer_profile_edit.html", profile=profile, form=values, levels=rp.LEVELS,
                           visibilities=rp.VISIBILITIES, stats=stats)


# ----------------------------------------------------------- the picker
def _picker_state(conn, task):
    """Currently selected reviewer ids: the session's working set, seeded from existing assignments."""
    key = f"pick:{task['id']}"
    if key not in session:
        session[key] = [r["reviewer_id"] for r in conn.execute(
            "SELECT reviewer_id FROM review_assignments WHERE workflow_id = ? AND round = ? AND status = 'assigned'",
            (task["workflow_id"], task["round"]))]
    return key, list(session[key])


def _picker_allowed(task):
    return task["current_state"] == WorkflowState.EVIDENCE_REQUIRED.value


@reviewers_bp.route("/tasks/<int:task_id>/reviewers")
@developer_required
def picker(task_id):
    filters = _filters(request.args)
    conn = get_db_connection()
    try:
        task = _owned_task_or_404(conn, task_id)
        if not _picker_allowed(task):
            flash("Reviewers can only be chosen while you are preparing evidence.", "error")
            return redirect(url_for("tasks.task_detail", task_id=task_id))
        key, selected_ids = _picker_state(conn, task)
        items, total, page, pages = rp.directory(conn, **filters)
        filters["page"] = page
        selected = []
        for uid in selected_ids:       # names for the selection panel, straight from the database
            row = conn.execute(
                "SELECT u.id, u.username, p.full_name FROM users u JOIN profiles p ON p.user_id = u.id "
                "WHERE u.id = ? AND u.role = 'reviewer' LIMIT 1", (uid,)).fetchone()
            if row:
                selected.append({"id": row["id"], "username": row["username"],
                                 "display_name": row["full_name"] or row["username"]})
        existing = conn.execute("SELECT required_approvals FROM workflow_instances WHERE id = ?",
                                (task["workflow_id"],)).fetchone()
    finally:
        conn.close()

    default = current_app.config["DEFAULT_REQUIRED_APPROVALS"]
    n = len(selected)
    try:
        required = int(request.args.get("required") or 0)
    except ValueError:
        required = 0
    if not required:
        required = (existing["required_approvals"] if n and existing and existing["required_approvals"] <= n
                    else min(default, max(n, 1)))
    problem = None
    if n == 0:
        problem = "Select at least one reviewer."
    elif required < 1 or required > n:
        problem = f"The number required must be between 1 and {n} (the number of selected reviewers)."
    return render_template("reviewer_picker.html", task=task, items=items, total=total, page=page, pages=pages,
                           filters=filters, levels=rp.LEVELS, selected=selected, selected_ids=set(selected_ids),
                           required=required, problem=problem, max_selected=MAX_SELECTED, default_required=default)


@reviewers_bp.route("/tasks/<int:task_id>/reviewers/select", methods=["POST"])
@developer_required
def picker_select(task_id):
    """Add or remove one reviewer from the working selection (kept in the session until saved)."""
    conn = get_db_connection()
    try:
        task = _owned_task_or_404(conn, task_id)
        if not _picker_allowed(task):
            abort(404)
        key, ids = _picker_state(conn, task)
        action = request.form.get("action")
        if action == "add":
            try:
                rp.selectable_reviewers(conn, [request.form.get("reviewer_id")], g.user["id"])
            except rp.ProfileError as error:
                flash_errors(error.problems)
            else:
                uid = int(request.form["reviewer_id"])
                if uid in ids:
                    flash("That reviewer is already selected.", "error")
                elif len(ids) >= MAX_SELECTED:
                    flash(f"You can select at most {MAX_SELECTED} reviewers.", "error")
                else:
                    ids.append(uid)
        elif action == "remove":
            try:
                ids = [i for i in ids if i != int(request.form.get("reviewer_id"))]
            except (TypeError, ValueError):
                abort(400)
        else:
            abort(400)
        session[key] = ids
    finally:
        conn.close()
    params = {k: v for k, v in _filters(request.form).items() if v and not (k == "page" and v == 1)}
    required = request.form.get("required", "")
    if required.isdigit():
        params["required"] = required
    return redirect(url_for("reviewers.picker", task_id=task_id, **params))
