"""Progress across versions: how findings moved from one round to the next.

Read-only and derived entirely from recorded data (versions, reports, findings and
their update chains). It produces counts and links, never a score or a grade.
A reviewer sees only the findings from their own reports; the author sees all.
"""
from services import finding_service as fs

# display groups for the status of a finding
GROUPS = (
    ("unresolved", "Unresolved", ("open", "acknowledged", "reopened")),
    ("awaiting", "Fixed, awaiting verification", ("fixed",)),
    ("resolved", "Resolved (verified by the reviewer)", ("confirmed",)),
    ("accepted", "Accepted as unresolved", ("wont_fix",)),
)
_GROUP_OF = {status: key for key, _, members in GROUPS for status in members}
SEVERITY_ORDER = ("critical", "high", "medium", "low")


def build_progress(conn, workflow_id, viewer_id, is_owner):
    versions = conn.execute("SELECT round, commit_sha, created_at FROM submission_versions "
                            "WHERE workflow_id = ? ORDER BY round", (workflow_id,)).fetchall()
    rows = conn.execute(
        "SELECT f.*, r.round AS round, r.reviewer_id, u.username AS reviewer_name "
        "FROM review_findings f JOIN review_reports r ON r.id = f.report_id "
        "JOIN users u ON u.id = r.reviewer_id WHERE r.workflow_id = ? ORDER BY r.round, r.id, f.seq",
        (workflow_id,)).fetchall()
    findings = {}
    for row in rows:
        if not (is_owner or row["reviewer_id"] == viewer_id):
            continue
        d = dict(row)
        d["status"] = fs.current_status(conn, row["id"])
        d["status_label"] = fs.STATUS_LABELS[d["status"]]
        d["group"] = _GROUP_OF[d["status"]]
        findings[d["id"]] = d

    follow_ups = {}
    for f in findings.values():
        if f["prior_finding_id"] in findings:
            follow_ups.setdefault(f["prior_finding_id"], []).append(f["id"])

    rounds = []
    for v in versions:
        mine = [f for f in findings.values() if f["round"] == v["round"]]
        counts = {key: 0 for key, _, _ in GROUPS}
        for f in mine:
            counts[f["group"]] += 1
        rounds.append({"round": v["round"], "commit_sha": v["commit_sha"], "created_at": v["created_at"],
                       "findings": mine, "total": len(mine), "counts": counts,
                       "reports": len({f["report_id"] for f in mine})})

    total = len(findings)
    overall = {key: sum(1 for f in findings.values() if f["group"] == key) for key, _, _ in GROUPS}
    open_serious = sorted((f for f in findings.values()
                           if f["group"] == "unresolved" and f["severity"] in ("critical", "high")),
                          key=lambda f: (SEVERITY_ORDER.index(f["severity"]), f["round"], f["id"]))
    return {"rounds": rounds, "total": total, "overall": overall, "groups": GROUPS,
            "follow_ups": follow_ups, "by_id": findings, "open_serious": open_serious}
