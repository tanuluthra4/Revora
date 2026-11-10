"""Structured, plain-text review reports: constants and server-side validation.

Nothing here fetches a URL or runs submitted code. A report is text written by
a community reviewer; it carries no verified-expertise claim.
"""
import re

CONSENT_VERSION = "v1"
CONSENT_TEXT = (
    "If the student chooses to share this version, the non-private text of this report "
    "and all of its findings may be published on a public page. Private notes are never "
    "published. I understand and agree."
)

SEVERITIES = ("critical", "high", "medium", "low")
CATEGORIES = {
    "production": ("security", "correctness", "reliability", "performance", "maintainability",
                   "testing", "documentation", "deployment", "accessibility", "data-handling"),
    "interview": ("explainability", "design-decisions", "depth-of-knowledge", "gaps",
                  "communication", "code-quality", "testing", "scope"),
}
GOALS = tuple(CATEGORIES)

LIMITS = {"summary": 3000, "strengths": 3000, "unresolved": 3000, "assessed": 2000,
          "not_assessed": 2000, "interview_qs": 3000, "private_notes": 3000,
          "title": 200, "explanation": 3000, "suggested_fix": 3000, "reasoning": 2000,
          "file_path": 300, "repro_evidence": 2000}
MAX_FINDINGS = 40
_SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")
_TRUE = {"1", "true", "on", "yes"}


class ReportInvalid(ValueError):
    def __init__(self, problems):
        super().__init__(" ".join(problems))
        self.problems = problems


def _clean(value):
    value = (value or "")
    value = "".join(ch for ch in str(value) if ch in "\n\t" or ord(ch) >= 32)
    return value.strip()


def consent_given(extra):
    """True only for an explicit acknowledgment of the CURRENT disclosure version."""
    return (str(extra.get("consent_ack", "")).strip().lower() in _TRUE
            and str(extra.get("consent_version", "")).strip() == CONSENT_VERSION)


def validate_report(conn, workflow, review_goal, extra):
    """Normalise and validate a posted report. Raises ReportInvalid.

    The consent check comes first: without an explicit acknowledgment of the
    current disclosure nothing else is looked at and nothing is stored.
    """
    if not consent_given(extra):
        raise ReportInvalid([
            "You must acknowledge the disclosure terms before submitting: the student may "
            "publish this report's non-private text and findings if they choose to share it."])

    raw = extra.get("report") or {}
    problems, report = [], {}
    for field, label, required in (
            ("summary", "Summary", True), ("strengths", "Strengths", True),
            ("assessed", "What you assessed", True), ("not_assessed", "What you did NOT assess", True),
            ("unresolved", "Unresolved concerns", False), ("interview_qs", "Interview questions", False)):
        text = _clean(raw.get(field))
        if required and not text:
            problems.append(f"{label} is required.")
        if len(text) > LIMITS[field]:
            problems.append(f"{label} can be at most {LIMITS[field]} characters.")
        report[field] = text or None
    if report["interview_qs"] and review_goal != "interview":
        problems.append("Interview questions apply only to interview-preparation reviews.")
    private = _clean(extra.get("private_notes"))
    if len(private) > LIMITS["private_notes"]:
        problems.append("Private notes are too long.")

    findings_in = extra.get("findings") or []
    if len(findings_in) > MAX_FINDINGS:
        problems.append(f"At most {MAX_FINDINGS} findings per report.")
    findings = []
    for i, item in enumerate(findings_in[:MAX_FINDINGS], start=1):
        f = {"seq": i}
        f["severity"] = _clean(item.get("severity")).lower()
        f["category"] = _clean(item.get("category")).lower()
        if f["severity"] not in SEVERITIES:
            problems.append(f"Finding {i}: choose a severity.")
        if f["category"] not in CATEGORIES[review_goal]:
            problems.append(f"Finding {i}: choose a category from the list.")
        for field, required in (("title", True), ("explanation", True), ("suggested_fix", True),
                                ("reasoning", False), ("file_path", False), ("repro_evidence", False)):
            text = _clean(item.get(field))
            if required and not text:
                problems.append(f"Finding {i}: {field.replace('_', ' ')} is required.")
            if len(text) > LIMITS[field]:
                problems.append(f"Finding {i}: {field.replace('_', ' ')} is too long.")
            f[field] = text or None
        ref = _clean(item.get("commit_ref"))
        if ref and not _SHA.match(ref):
            problems.append(f"Finding {i}: commit reference must be 7-40 hex characters.")
        f["commit_ref"] = ref.lower() or None
        prior = item.get("prior_finding_id")
        prior = int(prior) if str(prior or "").strip().isdigit() else None
        if prior is not None and conn.execute(
                "SELECT 1 FROM review_findings f JOIN review_reports r ON r.id = f.report_id "
                "WHERE f.id = ? AND r.workflow_id = ? AND r.round < ?",
                (prior, workflow["id"], workflow["round"])).fetchone() is None:
            problems.append(f"Finding {i}: the earlier finding it refers to was not found.")
        f["prior_finding_id"] = prior
        findings.append(f)
    if problems:
        raise ReportInvalid(problems)
    return {"report": report, "findings": findings, "private_notes": private or None}
