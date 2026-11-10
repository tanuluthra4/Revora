"""Opt-in, revocable public links (table `share_links`).

* private by default; a link exists only after the owner creates it;
* one active link per (workflow, round, kind); a revoked token is never reused:
  sharing again creates a NEW token;
* resolving a revoked or unknown token returns no data at all.

For the older 'proof' links the legacy columns on workflow_instances are kept in
step so a rollback to the previous release still sees the same links.
"""
import secrets

from database.db import transaction


def active_link(conn, workflow_id, round_no, kind):
    return conn.execute(
        "SELECT * FROM share_links WHERE workflow_id = ? AND round = ? AND kind = ? AND revoked_at IS NULL",
        (workflow_id, round_no, kind)).fetchone()


def has_revoked(conn, workflow_id, round_no, kind):
    return conn.execute(
        "SELECT 1 FROM share_links WHERE workflow_id = ? AND round = ? AND kind = ? AND revoked_at IS NOT NULL",
        (workflow_id, round_no, kind)).fetchone() is not None


def _mirror(conn, workflow_id, token, revoked):
    conn.execute("UPDATE workflow_instances SET share_token = ?, share_revoked_at = "
                 + ("CURRENT_TIMESTAMP" if revoked else "NULL") + " WHERE id = ?", (token, workflow_id))


def create_link(conn, workflow_id, round_no, kind, user_id):
    """Return the active token, creating one if needed. Call inside the caller's rules."""
    with transaction(conn):
        existing = active_link(conn, workflow_id, round_no, kind)
        if existing:
            return existing["token"]
        token = secrets.token_urlsafe(24)
        conn.execute("INSERT INTO share_links (workflow_id, round, kind, token, created_by) "
                     "VALUES (?, ?, ?, ?, ?)", (workflow_id, round_no, kind, token, user_id))
        if kind == "proof":
            _mirror(conn, workflow_id, token, revoked=False)
        return token


def revoke_link(conn, workflow_id, round_no, kind):
    with transaction(conn):
        cur = conn.execute(
            "UPDATE share_links SET revoked_at = CURRENT_TIMESTAMP WHERE workflow_id = ? AND round = ? "
            "AND kind = ? AND revoked_at IS NULL", (workflow_id, round_no, kind))
        if cur.rowcount and kind == "proof":
            token = conn.execute("SELECT token FROM share_links WHERE workflow_id = ? AND round = ? AND "
                                 "kind = 'proof' ORDER BY id DESC LIMIT 1", (workflow_id, round_no)).fetchone()
            _mirror(conn, workflow_id, token["token"], revoked=True)
        return cur.rowcount


def resolve(conn, token, kind):
    """-> (status, row). status is 'active', 'revoked' or 'unknown'; row only when active."""
    link = conn.execute("SELECT * FROM share_links WHERE token = ? AND kind = ?", (token, kind)).fetchone()
    if link is None:
        return "unknown", None
    if link["revoked_at"]:
        return "revoked", None
    return "active", link
