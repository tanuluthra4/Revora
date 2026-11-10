"""Public, read-only pages. Nothing here is visible unless the developer
explicitly created a share link for a VERIFIED proof; revoking the link stops
exposing the proof immediately. Reviewer e-mails, private notes, internal ids
and session data are never rendered here."""
import json

from flask import Blueprint, abort, render_template

from database.db import get_db_connection
from services.evidence_service import EVIDENCE_TYPES
from services.integrity import verify_chain
from services.review_service import public_summary
from services.share_service import resolve

public_bp = Blueprint("public", __name__)


@public_bp.route("/proof/<token>")
def proof(token):
    conn = get_db_connection()
    try:
        status, link = resolve(conn, token, "proof")
        if status == "unknown":
            abort(404)
        row = None
        if link is not None:
            row = conn.execute(
                """
                SELECT w.id AS workflow_id, w.round, w.current_state, w.completed_at, w.chain_head,
                       w.required_approvals, w.review_mode,
                       t.title, t.description, p.title AS project_title, u.username AS owner_name
                FROM workflow_instances w
                JOIN tasks t ON t.id = w.task_id
                JOIN users u ON u.id = w.user_id
                LEFT JOIN projects p ON p.id = t.project_id
                WHERE w.id = ?
                """,
                (link["workflow_id"],),
            ).fetchone()

        if status == "revoked":
            # No title, evidence or names: a revoked link exposes nothing.
            return render_template("proof_unavailable.html", reason="revoked"), 410
        if row is None or row["current_state"] != "VERIFIED" or row["review_mode"] == "review":
            return render_template("proof_unavailable.html", reason="not_verified"), 404

        evidence = conn.execute(
            """
            SELECT evidence_type, evidence_url, title, description
            FROM evidence
            WHERE workflow_id = ? AND round = ? AND verification_status = 'approved'
            ORDER BY id
            """,
            (row["workflow_id"], row["round"]),
        ).fetchall()
        submitted = conn.execute(
            "SELECT created_at, evidence_digest, meta FROM state_transitions "
            "WHERE workflow_id = ? AND event = 'SUBMIT_EVIDENCE' ORDER BY seq DESC LIMIT 1",
            (row["workflow_id"],),
        ).fetchone()
        reviewers = public_summary(conn, row["workflow_id"], row["round"])
        chain = verify_chain(conn, row["workflow_id"])
    finally:
        conn.close()

    policy = None
    if submitted and submitted["meta"]:
        try:
            policy = json.loads(submitted["meta"])
        except ValueError:
            policy = None
    approvals = sum(1 for r in reviewers if r["decision"] == "approve")
    return render_template(
        "proof.html", proof=row, evidence=evidence, reviewers=reviewers, approvals=approvals,
        required=row["required_approvals"], assigned=len(policy["reviewers"]) if policy else len(reviewers),
        submitted_at=submitted["created_at"] if submitted else None,
        version=(submitted["evidence_digest"][:12] if submitted and submitted["evidence_digest"] else None),
        chain=chain, evidence_types=EVIDENCE_TYPES,
    )


@public_bp.route("/u/<username>")
def profile(username):
    conn = get_db_connection()
    try:
        owner = conn.execute("SELECT id, username FROM users WHERE username = ? AND role = 'developer'",
                             (username,)).fetchone()
        if owner is None:
            abort(404)
        proofs = conn.execute(
            """
            SELECT t.title, s.token AS share_token, w.completed_at, w.required_approvals,
                   (SELECT COUNT(*) FROM review_decisions d
                     WHERE d.workflow_id = w.id AND d.round = w.round AND d.decision = 'approve') AS approvals,
                   (SELECT COUNT(*) FROM review_assignments x
                     WHERE x.workflow_id = w.id AND x.round = w.round AND x.status = 'assigned') AS reviewer_count
            FROM workflow_instances w
            JOIN tasks t ON t.id = w.task_id
            JOIN share_links s ON s.workflow_id = w.id AND s.round = w.round AND s.kind = 'proof'
                              AND s.revoked_at IS NULL
            WHERE w.user_id = ? AND w.current_state = 'VERIFIED'
            ORDER BY w.completed_at DESC
            """,
            (owner["id"],),
        ).fetchall()
    finally:
        conn.close()
    return render_template("profile.html", owner=owner, proofs=proofs)
