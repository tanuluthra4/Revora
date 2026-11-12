"""Revora review requests: versions, reports, findings and opt-in public sharing.

Authorisation is decided on the server in every view:
  * a review request belongs to its owner; reviewers see it only while assigned;
  * a reviewer sees their OWN report (and private notes), never another reviewer's;
  * the owner sees every report but never a reviewer's private notes;
  * nothing is public until the owner creates a share link for one version.
"""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from automata.states import WorkflowEvent, WorkflowState
from automata.transitions import events_available_to
from database.db import get_db_connection
from routes.helpers import developer_required, flash_errors, login_required
from routes.tasks import _access_for, _load_task, _owned_task_or_404, _user_projects, engine
from services import finding_service as fs
from services.evidence_service import EvidenceError, EvidenceNotAllowed
from services.fsm_view import EVENT_LABELS, STATE_LABELS, build_fsm_view
from services.integrity import verify_chain
from services.progress_service import build_progress
from services.automata_engine import TransitionBlocked, WorkflowNotFound
from services.report_service import (CATEGORIES, CONSENT_TEXT, CONSENT_VERSION, MAX_FINDINGS, SEVERITIES)
from services.share_service import active_link, create_link, has_revoked, resolve, revoke_link
from services.version_service import GOAL_LABELS, create_review_request, get_version, update_version

reviews_bp = Blueprint("reviews", __name__)

FORM_FINDING_SLOTS = 8
REPORT_FIELDS = ("summary", "strengths", "unresolved", "assessed", "not_assessed", "interview_qs")
FINDING_FIELDS = ("severity", "category", "title", "explanation", "suggested_fix", "reasoning",
                  "file_path", "commit_ref", "repro_evidence", "prior_finding_id")
NOT_A_CERTIFICATION = (
    "This is a community review written by an individual account. It is not a certification, "
    "endorsement or guarantee. Revora has not verified any reviewer's identity, employer or "
    "expertise, and separate accounts do not prove that reviewers are independent of the author."
)


# --------------------------------------------------------------- new request
@reviews_bp.route("/reviews/new", methods=["GET", "POST"])
@developer_required
def new_request():
    conn = get_db_connection()
    try:
        projects = _user_projects(conn)
        form = request.form if request.method == "POST" else {"project_id": request.args.get("project_id", "")}
        if request.method == "POST":
            try:
                task_id, _ = create_review_request(conn, g.user["id"], request.form.get("project_id"),
                                                   request.form.get("title"), request.form)
                flash("Review request created. Choose reviewers, then submit this version.", "success")
                return redirect(url_for("reviews.detail", task_id=task_id))
            except EvidenceError as error:
                flash_errors(error.problems)
        return render_template("review_new.html", projects=projects, form=form, goals=GOAL_LABELS)
    finally:
        conn.close()


# -------------------------------------------------------------------- detail
def _reports_for_viewer(conn, task, is_owner):
    rows = conn.execute(
        "SELECT r.*, u.username FROM review_reports r JOIN users u ON u.id = r.reviewer_id "
        "WHERE r.workflow_id = ? ORDER BY r.round DESC, r.id", (task["workflow_id"],)).fetchall()
    out = []
    for r in rows:
        mine = r["reviewer_id"] == g.user["id"]
        if not (is_owner or mine):
            continue                      # reviewers never see each other's reports
        d = dict(r)
        if not mine:
            d["private_notes"] = None     # the owner never sees private notes
        role = "owner" if is_owner else "reviewer"
        d["findings"] = []
        for f in fs.findings_for_report(conn, r["id"]):
            f["next"] = fs.allowed_next(role, f["status"])
            f["updates"] = fs.finding_updates(conn, f["id"]) if (is_owner or mine) else []
            d["findings"].append(f)
        out.append(d)
    return out


@reviews_bp.route("/reviews/<int:task_id>")
@login_required
def detail(task_id):
    conn = get_db_connection()
    try:
        task = _load_task(conn, task_id)
        access = _access_for(conn, task)
        if access is None:
            abort(404)
        if task["review_mode"] != "review":
            return redirect(url_for("tasks.task_detail", task_id=task_id))
        is_owner, is_reviewer = access == "owner", access == "reviewer"
        wid, rnd = task["workflow_id"], task["round"]
        state = WorkflowState(task["current_state"])
        version = get_version(conn, wid, rnd)
        versions = conn.execute("SELECT v.*, p.title AS project_title FROM submission_versions v "
                                "JOIN projects p ON p.id = v.project_id WHERE v.workflow_id = ? "
                                "ORDER BY v.round DESC", (wid,)).fetchall()
        assigned = conn.execute(
            "SELECT a.reviewer_id, u.username, "
            "EXISTS(SELECT 1 FROM review_reports r WHERE r.workflow_id = a.workflow_id AND r.round = a.round "
            "AND r.reviewer_id = a.reviewer_id) AS reported "
            "FROM review_assignments a JOIN users u ON u.id = a.reviewer_id "
            "WHERE a.workflow_id = ? AND a.round = ? AND a.status = 'assigned' ORDER BY u.username",
            (wid, rnd)).fetchall()
        my_report = conn.execute("SELECT 1 FROM review_reports WHERE workflow_id = ? AND round = ? "
                                 "AND reviewer_id = ?", (wid, rnd, g.user["id"])).fetchone()
        history = engine.get_history(wid)
        actions = [e for e in events_available_to(state, is_owner, is_reviewer)
                   if e not in (WorkflowEvent.REASSIGN_REVIEWER, WorkflowEvent.WITHDRAW,
                                WorkflowEvent.SUBMIT_REPORT)]
        fsm = build_fsm_view(task["current_state"], history, actions)
        reports = _reports_for_viewer(conn, task, is_owner)

        # earlier findings by this reviewer, so a follow-up finding can point at one
        prior = []
        if is_reviewer:
            prior = conn.execute(
                "SELECT f.id, f.title, r.round FROM review_findings f JOIN review_reports r ON r.id = f.report_id "
                "WHERE r.workflow_id = ? AND r.reviewer_id = ? AND r.round < ? ORDER BY r.round, f.seq",
                (wid, g.user["id"], rnd)).fetchall()

        shares = []
        if is_owner:
            for v in versions:
                n = conn.execute("SELECT COUNT(*) FROM review_reports WHERE workflow_id = ? AND round = ?",
                                 (wid, v["round"])).fetchone()[0]
                complete = v["round"] < rnd or state == WorkflowState.REVIEWED
                link = active_link(conn, wid, v["round"], "report")
                shares.append({"round": v["round"], "reports": n, "can_share": n > 0 and complete,
                               "token": link["token"] if link else None,
                               "url": url_for("reviews.public_report", token=link["token"], _external=True) if link else None,
                               "revoked": (not link) and has_revoked(conn, wid, v["round"], "report")})

        can_choose = is_owner and state == WorkflowState.EVIDENCE_REQUIRED
        in_review = task["current_state"] in ("EVIDENCE_SUBMITTED", "VALIDATING")
        return render_template(
            "review_detail.html", task=task, version=version, versions=versions, is_owner=is_owner,
            is_reviewer=is_reviewer, state=state, state_label=STATE_LABELS[state], actions=actions,
            event_labels=EVENT_LABELS, fsm=fsm, history=history, chain=verify_chain(conn, wid),
            blocked=engine.get_blocked_actions(wid) if is_owner else [], assigned=assigned,
            reports=reports, shares=shares, goal_labels=GOAL_LABELS, prior=prior,
            can_edit_version=is_owner and task["current_state"] in ("CREATED", "IN_PROGRESS", "EVIDENCE_REQUIRED")
            and version is not None and version["locked_at"] is None,
            can_choose=can_choose, can_withdraw=is_owner and in_review,
            can_reassign=is_owner and in_review,
            can_report=is_reviewer and state == WorkflowState.VALIDATING and not my_report,
            my_report=bool(my_report),
            categories=CATEGORIES.get(version["review_goal"] if version else "production"),
            severities=SEVERITIES, consent_text=CONSENT_TEXT, consent_version=CONSENT_VERSION,
            slots=range(1, FORM_FINDING_SLOTS + 1), status_labels=fs.STATUS_LABELS,
            disclaimer=NOT_A_CERTIFICATION)
    finally:
        conn.close()


@reviews_bp.route("/reviews/<int:task_id>/progress")
@login_required
def progress(task_id):
    conn = get_db_connection()
    try:
        task = _load_task(conn, task_id)
        access = _access_for(conn, task)
        if access is None or task["review_mode"] != "review":
            abort(404)
        data = build_progress(conn, task["workflow_id"], g.user["id"], access == "owner")
        return render_template("review_progress.html", task=task, data=data, is_owner=access == "owner")
    finally:
        conn.close()


# ------------------------------------------------------------------ version
@reviews_bp.route("/reviews/<int:task_id>/version", methods=["POST"])
@developer_required
def edit_version(task_id):
    conn = get_db_connection()
    try:
        task = _owned_task_or_404(conn, task_id)
        try:
            update_version(conn, task["workflow_id"], g.user["id"], request.form)
            flash("Version details saved.", "success")
        except EvidenceError as error:
            flash_errors(error.problems)
        except EvidenceNotAllowed as error:
            flash(str(error), "error")
    finally:
        conn.close()
    return redirect(url_for("reviews.detail", task_id=task_id))


# ------------------------------------------------------------------- report
def _parse_report_form(form):
    findings = []
    for i in range(1, MAX_FINDINGS + 1):
        item = {f: form.get(f"f{i}_{f}", "") for f in FINDING_FIELDS}
        if not any(str(v).strip() for v in item.values()):
            continue                        # an untouched slot is not a finding
        findings.append(item)
    return {"report": {f: form.get(f, "") for f in REPORT_FIELDS},
            "findings": findings, "private_notes": form.get("private_notes", ""),
            "consent_ack": form.get("consent_ack"), "consent_version": form.get("consent_version")}


@reviews_bp.route("/reviews/<int:task_id>/report", methods=["POST"])
@login_required
def submit_report(task_id):
    conn = get_db_connection()
    try:
        task = _load_task(conn, task_id)
        if _access_for(conn, task) is None or task["review_mode"] != "review":
            abort(404)
    finally:
        conn.close()
    try:
        engine.transition(task["workflow_id"], WorkflowEvent.SUBMIT_REPORT, g.user["id"], None,
                          _parse_report_form(request.form))
        flash("Report submitted. The author can now read it.", "success")
    except TransitionBlocked as blocked:
        flash(f"Report not submitted ({blocked.code}): {blocked}", "error")
    except WorkflowNotFound:
        abort(404)
    return redirect(url_for("reviews.detail", task_id=task_id))


# ----------------------------------------------------------------- findings
@reviews_bp.route("/findings/<int:finding_id>/update", methods=["POST"])
@login_required
def update_finding(finding_id):
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT r.workflow_id, w.task_id FROM review_findings f "
                           "JOIN review_reports r ON r.id = f.report_id "
                           "JOIN workflow_instances w ON w.id = r.workflow_id WHERE f.id = ?",
                           (finding_id,)).fetchone()
        if row is None or fs.actor_role_for(conn, finding_id, g.user["id"]) is None:
            abort(404)
        try:
            fs.update_finding(conn, finding_id, g.user["id"], request.form.get("to_status"),
                              request.form.get("note"), request.form.get("commit_ref"))
            flash("Finding updated.", "success")
        except fs.FindingError as error:
            flash(str(error), "error")
        except fs.FindingNotAllowed:
            abort(404)
    finally:
        conn.close()
    return redirect(url_for("reviews.detail", task_id=row["task_id"]) + f"#finding-{finding_id}")


# ------------------------------------------------------------------ sharing
@reviews_bp.route("/reviews/<int:task_id>/share", methods=["POST"])
@developer_required
def share_report(task_id):
    conn = get_db_connection()
    try:
        task = _owned_task_or_404(conn, task_id)
        if task["review_mode"] != "review":
            abort(404)
        try:
            rnd = int(request.form.get("round", ""))
        except ValueError:
            abort(400)
        reports = conn.execute("SELECT COUNT(*) FROM review_reports WHERE workflow_id = ? AND round = ?",
                               (task["workflow_id"], rnd)).fetchone()[0]
        complete = rnd < task["round"] or task["current_state"] == "REVIEWED"
        if rnd < 1 or rnd > task["round"] or reports == 0 or not complete:
            flash("Only a completed review of a version can be shared.", "error")
        else:
            create_link(conn, task["workflow_id"], rnd, "report", g.user["id"])
            flash("Public link created for this version. Anyone with the link can read its reports "
                  "and findings (private notes are never included).", "success")
    finally:
        conn.close()
    return redirect(url_for("reviews.detail", task_id=task_id))


@reviews_bp.route("/reviews/<int:task_id>/unshare", methods=["POST"])
@developer_required
def unshare_report(task_id):
    conn = get_db_connection()
    try:
        task = _owned_task_or_404(conn, task_id)
        try:
            rnd = int(request.form.get("round", ""))
        except ValueError:
            abort(400)
        revoke_link(conn, task["workflow_id"], rnd, "report")
        flash("Public link revoked. It no longer shows anything.", "success")
    finally:
        conn.close()
    return redirect(url_for("reviews.detail", task_id=task_id))


@reviews_bp.route("/r/<token>")
def public_report(token):
    conn = get_db_connection()
    try:
        status, link = resolve(conn, token, "report")
        if status == "unknown":
            abort(404)
        if status == "revoked":
            # No title, names or findings: a revoked link exposes nothing.
            response = render_template("review_unavailable.html"), 410
            return response
        wid, rnd = link["workflow_id"], link["round"]
        version = conn.execute(
            "SELECT v.review_goal, v.repo_url, v.commit_sha, v.tech_stack, p.title AS project_title, "
            "u.username AS owner_name FROM submission_versions v JOIN projects p ON p.id = v.project_id "
            "JOIN workflow_instances w ON w.id = v.workflow_id JOIN users u ON u.id = w.user_id "
            "WHERE v.workflow_id = ? AND v.round = ?", (wid, rnd)).fetchone()
        if version is None:
            abort(404)
        reports = []
        for r in conn.execute(
                "SELECT r.id, r.summary, r.strengths, r.unresolved, r.assessed, r.not_assessed, "
                "r.interview_qs, r.created_at, u.username, pr.profile_visibility, pr.full_name "
                "FROM review_reports r JOIN users u ON u.id = r.reviewer_id "
                "LEFT JOIN profiles pr ON pr.user_id = u.id WHERE r.workflow_id = ? AND r.round = ? "
                "ORDER BY r.id", (wid, rnd)):
            d = dict(r)
            d["profile_public"] = r["profile_visibility"] in ("listed", "unlisted")
            d["findings"] = fs.findings_for_report(conn, r["id"])
            reports.append(d)
        chain = verify_chain(conn, wid)
    finally:
        conn.close()
    return render_template("review_public.html", version=version, reports=reports, round=rnd, chain=chain,
                           goal_labels=GOAL_LABELS, disclaimer=NOT_A_CERTIFICATION)
