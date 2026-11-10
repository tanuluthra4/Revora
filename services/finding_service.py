"""Finding lifecycle: who may move a finding to which status.

Every change is an append-only `finding_updates` row in a per-finding hash
chain. The finding's current status is the `to_status` of its last update
(or 'open' when there is none). Reports and findings themselves never change.
"""
import hashlib
import json
from datetime import datetime, timezone

from database.db import transaction

GENESIS = "0" * 64
# Stored values are stable (they are hashed into history); only the labels shown to people differ.
STATUS_LABELS = {
    "open": "Open",
    "acknowledged": "In progress",
    "fixed": "Fixed \u2014 awaiting verification",
    "confirmed": "Resolved \u2014 verified by the reviewer",
    "wont_fix": "Accepted \u2014 unresolved",
    "reopened": "Reopened \u2014 still unresolved",
}
# (role, from_status) -> allowed to_status
RULES = {
    ("owner", "open"): ("acknowledged", "fixed", "wont_fix"),
    ("owner", "reopened"): ("acknowledged", "fixed", "wont_fix"),
    ("owner", "acknowledged"): ("fixed", "wont_fix"),
    ("owner", "wont_fix"): ("acknowledged",),
    ("reviewer", "fixed"): ("confirmed", "reopened"),
}
MAX_NOTE = 1000


class FindingError(ValueError):
    pass


class FindingNotAllowed(PermissionError):
    pass


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def update_hash(row):
    payload = [row["finding_id"], row["seq"], row["actor_id"], row["actor_role"], row["from_status"],
               row["to_status"], row["note"], row["commit_ref"], row["created_at"], row["prev_hash"]]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()


def current_status(conn, finding_id):
    row = conn.execute("SELECT to_status FROM finding_updates WHERE finding_id = ? ORDER BY seq DESC LIMIT 1",
                       (finding_id,)).fetchone()
    return row["to_status"] if row else "open"


def allowed_next(role, status):
    return RULES.get((role, status), ())


def actor_role_for(conn, finding_id, user_id):
    """'owner', 'reviewer' (author of the report) or None. Never trusts the client."""
    row = conn.execute(
        "SELECT w.user_id AS owner_id, r.reviewer_id FROM review_findings f "
        "JOIN review_reports r ON r.id = f.report_id "
        "JOIN workflow_instances w ON w.id = r.workflow_id WHERE f.id = ?", (finding_id,)).fetchone()
    if row is None:
        return None
    if row["owner_id"] == user_id:
        return "owner"
    if row["reviewer_id"] == user_id:
        return "reviewer"
    return None


def update_finding(conn, finding_id, user_id, to_status, note, commit_ref=None):
    note = (note or "").strip()
    commit_ref = (commit_ref or "").strip().lower() or None
    if len(note) < 3:
        raise FindingError("Add a short note explaining the change.")
    if len(note) > MAX_NOTE:
        raise FindingError(f"Notes can be at most {MAX_NOTE} characters.")
    if commit_ref and not (7 <= len(commit_ref) <= 40 and all(c in "0123456789abcdef" for c in commit_ref)):
        raise FindingError("Commit reference must be 7-40 hexadecimal characters.")
    with transaction(conn):
        role = actor_role_for(conn, finding_id, user_id)
        if role is None:
            raise FindingNotAllowed("Finding not found.")
        status = current_status(conn, finding_id)
        if to_status not in allowed_next(role, status):
            raise FindingError(f"A {role} cannot move a finding from '{status}' to '{to_status}'.")
        last = conn.execute("SELECT seq, hash FROM finding_updates WHERE finding_id = ? "
                            "ORDER BY seq DESC LIMIT 1", (finding_id,)).fetchone()
        row = {"finding_id": finding_id, "seq": (last["seq"] + 1) if last else 1, "actor_id": user_id,
               "actor_role": role, "from_status": status, "to_status": to_status, "note": note,
               "commit_ref": commit_ref, "created_at": _now(),
               "prev_hash": last["hash"] if last else GENESIS}
        row["hash"] = update_hash(row)
        conn.execute(
            "INSERT INTO finding_updates (finding_id, seq, actor_id, actor_role, from_status, to_status, "
            "note, commit_ref, created_at, prev_hash, hash) VALUES (:finding_id, :seq, :actor_id, "
            ":actor_role, :from_status, :to_status, :note, :commit_ref, :created_at, :prev_hash, :hash)", row)
    return to_status


def verify_findings(conn, workflow_id):
    """Check every finding's update chain. Returns None, or (finding_id, message)."""
    for f in conn.execute(
            "SELECT f.id FROM review_findings f JOIN review_reports r ON r.id = f.report_id "
            "WHERE r.workflow_id = ?", (workflow_id,)):
        prev, status, expected = GENESIS, "open", 1
        for u in conn.execute("SELECT * FROM finding_updates WHERE finding_id = ? ORDER BY seq", (f["id"],)):
            if (u["seq"] != expected or u["prev_hash"] != prev or u["hash"] != update_hash(u)
                    or u["from_status"] != status
                    or u["to_status"] not in allowed_next(u["actor_role"], status)):
                return f["id"], f"The history of finding #{f['id']} is not valid at update {u['seq']}."
            prev, status, expected = u["hash"], u["to_status"], expected + 1
    return None


def findings_for_report(conn, report_id):
    rows = conn.execute("SELECT * FROM review_findings WHERE report_id = ? ORDER BY seq", (report_id,)).fetchall()
    out = []
    for f in rows:
        d = dict(f)
        d["status"] = current_status(conn, f["id"])
        d["status_label"] = STATUS_LABELS[d["status"]]
        out.append(d)
    return out


def finding_updates(conn, finding_id):
    return conn.execute(
        "SELECT u.*, us.username AS actor_name FROM finding_updates u JOIN users us ON us.id = u.actor_id "
        "WHERE u.finding_id = ? ORDER BY u.seq", (finding_id,)).fetchall()
