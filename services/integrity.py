"""Tamper-evident audit trail for workflow transitions.

Every row in `state_transitions` carries `prev_hash` and `hash`, where

    hash = SHA-256( canonical JSON of the row's fields + prev_hash )

so each entry commits to the whole history before it (a hash chain). Editing
any past row changes its hash and breaks the link to the next row.

`verify_chain` goes further than checking hashes: it *replays the recorded
events through the automaton*. A history is only accepted if

  1. the hash chain is intact,
  2. every step is an edge of the transition table (the word is accepted by
     the DFA from CREATED),
  3. the replayed final state equals the state stored on the workflow,
  4. the evidence that was submitted still matches the digest sealed at the
     moment of submission.

Limits (be honest in interviews): this is tamper-*evident*, not tamper-proof.
Someone with full write access to the SQLite file could recompute the whole
chain. Anchoring `chain_head` somewhere external (e.g. a public log) would
close that gap and is listed as future work.
"""
import hashlib
import json

from automata.states import WorkflowEvent, WorkflowState
from automata.transitions import INITIAL_STATE, TRANSITIONS

GENESIS_HASH = "0" * 64

HASH_FIELDS = (
    "workflow_id",
    "seq",
    "from_state",
    "event",
    "to_state",
    "condition_result",
    "reason",
    "actor_id",
    "actor_role",
    "evidence_digest",
    "created_at",
    "prev_hash",
)


def compute_hash(record) -> str:
    """SHA-256 over a canonical encoding of the transition's fields."""
    payload = {field: record[field] for field in HASH_FIELDS}
    # `meta` (reviewer policy, reassignment details) joins the hash only when
    # present, so chains written before multi-reviewer support still verify.
    try:
        meta = record["meta"]
    except (KeyError, IndexError):
        meta = None
    if meta is not None:
        payload["meta"] = meta
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def evidence_digest(evidence_rows) -> str:
    """Digest of the immutable fields of a set of evidence rows.

    Mutable columns (verification_status) are deliberately excluded so the
    digest sealed at submission time stays valid after a review decision.
    """
    items = [
        [
            row["id"],
            row["evidence_type"],
            row["evidence_url"],
            row["title"],
            row["description"],
            row["created_at"],
        ]
        for row in sorted(evidence_rows, key=lambda r: r["id"])
    ]
    encoded = json.dumps(items, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def version_digest(project_id, repo_url, commit_sha, demo_url, tech_stack, review_goal, questions):
    """Digest of one submitted project version (sealed into SUBMIT_EVIDENCE for review mode)."""
    encoded = json.dumps([project_id, repo_url, commit_sha, demo_url, tech_stack,
                          review_goal, questions], separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


REPORT_FIELDS = ("summary", "strengths", "unresolved", "assessed", "not_assessed", "interview_qs")
FINDING_FIELDS = ("severity", "category", "title", "explanation", "suggested_fix", "reasoning",
                  "file_path", "commit_ref", "repro_evidence", "prior_finding_id")


def report_digest(round_no, reviewer_id, review_goal, consent_version, report, findings):
    """Digest of everything in a report that can be shown publicly.

    Private notes are deliberately excluded. `consent_version` IS included, so a
    report whose disclosure acknowledgment was removed or changed no longer
    matches the digest sealed in the audit chain.
    """
    payload = [
        round_no, reviewer_id, review_goal, consent_version,
        [report[f] for f in REPORT_FIELDS],
        [[f_["seq"]] + [f_[k] for k in FINDING_FIELDS]
         for f_ in sorted(findings, key=lambda f_: f_["seq"])],
    ]
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def _load_meta(row):
    try:
        return json.loads(row["meta"]) if row["meta"] else None
    except ValueError:
        return None


def _check_review_rules(conn, row, event, workflow_id, evidence_round, policy,
                        approvals, rejections, submitted_digest):
    """Replay the multi-reviewer rules for one link. Returns (code, message) or None."""
    seq = row["seq"]
    if event == WorkflowEvent.SUBMIT_EVIDENCE and row["actor_role"] != "legacy":
        meta = _load_meta(row)
        if meta is not None and not (
                isinstance(meta.get("reviewers"), list) and meta["reviewers"]
                and isinstance(meta.get("required"), int)
                and 1 <= meta["required"] <= len(meta["reviewers"])
                and len(set(meta["reviewers"])) == len(meta["reviewers"])):
            return ("BAD_POLICY", f"Link #{seq} seals an impossible approval policy.")

    if policy is None:
        return None          # legacy submission: no sealed policy to enforce

    if event == WorkflowEvent.REASSIGN_REVIEWER:
        meta = _load_meta(row) or {}
        remove, add = meta.get("remove"), meta.get("add")
        if (remove not in policy["reviewers"] or add in policy["reviewers"]
                or remove in approvals or remove in rejections or add == row["actor_id"]):
            return ("ILLEGAL_REASSIGNMENT", f"Link #{seq} is not a legal reviewer reassignment.")

    if event in (WorkflowEvent.VOTE_APPROVE, WorkflowEvent.VOTE_REJECT):
        voter = row["actor_id"]
        if voter not in policy["reviewers"]:
            return ("ILLEGAL_VOTE", f"Link #{seq} is a vote by someone who was not an assigned reviewer.")
        if voter in approvals or voter in rejections:
            return ("ILLEGAL_VOTE", f"Link #{seq} is a second vote by the same reviewer.")
        decision = conn.execute(
            "SELECT decision, notes, evidence_digest FROM review_decisions "
            "WHERE workflow_id = ? AND round = ? AND reviewer_id = ?",
            (workflow_id, evidence_round, voter),
        ).fetchone()
        wanted = "approve" if event == WorkflowEvent.VOTE_APPROVE else "reject"
        if (decision is None or decision["decision"] != wanted
                or decision["notes"] != row["reason"]
                or decision["evidence_digest"] != submitted_digest):
            return ("DECISION_MISMATCH",
                    f"The stored decision for link #{seq} does not match what the chain recorded "
                    "(decision, note or evidence version differs).")

    if event == WorkflowEvent.APPROVE and len(approvals) < policy["required"]:
        return ("QUORUM_NOT_MET",
                f"Verified with {len(approvals)} approvals but {policy['required']} were required.")
    if event == WorkflowEvent.REJECT and len(rejections) <= len(policy["reviewers"]) - policy["required"]:
        return ("QUORUM_NOT_MET", "Rejected although enough approvals were still possible.")
    return None


REVIEW_ONLY_EVENTS = frozenset({WorkflowEvent.SUBMIT_REPORT, WorkflowEvent.COMPLETE_REVIEW,
                                WorkflowEvent.NEW_VERSION})
VERIFICATION_ONLY_EVENTS = frozenset({WorkflowEvent.VOTE_APPROVE, WorkflowEvent.VOTE_REJECT,
                                      WorkflowEvent.APPROVE, WorkflowEvent.REJECT,
                                      WorkflowEvent.RESTART})


def _check_mode_rules(conn, workflow, row, event, evidence_round, policy, chain_mode, reporters):
    """Replay the review-mode rules for one link. Returns (code, message) or None.

    The mode of a request is sealed in SUBMIT_EVIDENCE's meta (absent = the
    original verification mode), so it cannot be flipped by editing a column.
    """
    seq = row["seq"]
    if event in REVIEW_ONLY_EVENTS and chain_mode != "review":
        return ("MODE_VIOLATION", f"Link #{seq} ({event.value}) exists only for review-mode requests.")
    if event in VERIFICATION_ONLY_EVENTS and chain_mode == "review":
        return ("MODE_VIOLATION", f"Link #{seq} ({event.value}) is not allowed in a review-mode request.")

    if event == WorkflowEvent.SUBMIT_EVIDENCE and row["actor_role"] != "legacy":
        meta = _load_meta(row) or {}
        mode = meta.get("mode", "verification")
        if mode not in ("verification", "review") or (chain_mode is not None and mode != chain_mode):
            return ("MODE_VIOLATION", f"Link #{seq} changes the mode of the request.")
        if mode == "review":
            version = conn.execute(
                "SELECT * FROM submission_versions WHERE workflow_id = ? AND round = ?",
                (workflow["id"], evidence_round)).fetchone()
            if version is None or version_digest(
                    version["project_id"], version["repo_url"], version["commit_sha"],
                    version["demo_url"], version["tech_stack"], version["review_goal"],
                    version["questions"]) != meta.get("version_digest") \
                    or version["review_goal"] != meta.get("goal"):
                return ("VERSION_ALTERED",
                        f"The version stored for link #{seq} no longer matches the digest sealed "
                        "when it was submitted.")

    if event == WorkflowEvent.REASSIGN_REVIEWER and policy is not None:
        remove = (_load_meta(row) or {}).get("remove")
        if remove in reporters:
            return ("ILLEGAL_REASSIGNMENT", f"Link #{seq} replaces a reviewer who had already reported.")

    if event == WorkflowEvent.SUBMIT_REPORT:
        who = row["actor_id"]
        if policy is None or who not in policy["reviewers"] or who == workflow["user_id"]:
            return ("ILLEGAL_REPORT", f"Link #{seq} is a report by someone who was not an assigned "
                                      "reviewer, or by the owner.")
        if who in reporters:
            return ("ILLEGAL_REPORT", f"Link #{seq} is a second report by the same reviewer.")
        if len(reporters) >= policy["required"]:
            return ("ILLEGAL_REPORT", f"Link #{seq} is a report submitted after the review was complete.")
        meta = _load_meta(row) or {}
        report = conn.execute(
            "SELECT * FROM review_reports WHERE workflow_id = ? AND round = ? AND reviewer_id = ?",
            (workflow["id"], evidence_round, who)).fetchone()
        if report is None:
            return ("REPORT_MISMATCH", f"Link #{seq} records a report that is not stored.")
        if not report["consent_ack_at"] or not report["consent_version"] \
                or report["consent_version"] != meta.get("consent_version"):
            return ("CONSENT_MISSING", f"The report recorded at link #{seq} has no valid "
                                       "disclosure acknowledgment.")
        findings = [dict(f_) for f_ in conn.execute(
            "SELECT * FROM review_findings WHERE report_id = ? ORDER BY seq", (report["id"],))]
        recomputed = report_digest(evidence_round, who, report["review_goal"],
                                   report["consent_version"], report, findings)
        if recomputed != report["content_digest"] or recomputed != meta.get("report_digest") \
                or report["review_goal"] != policy.get("goal"):
            return ("REPORT_MISMATCH", f"The stored report for link #{seq} does not match the "
                                       "digest sealed in the audit trail.")

    if event == WorkflowEvent.COMPLETE_REVIEW and policy is not None \
            and len(reporters) < policy["required"]:
        return ("QUORUM_NOT_MET", f"Review marked complete with {len(reporters)} reports but "
                                  f"{policy['required']} were required.")
    return None


def _result(ok, length, replayed_state=None, error=None, head=None):
    return {
        "ok": ok,
        "length": length,
        "replayed_state": replayed_state,
        "head": head,
        "error": error,
    }


def verify_chain(conn, workflow_id) -> dict:
    """Verify the audit trail of one workflow. Never raises on tampered data."""
    workflow = conn.execute(
        "SELECT * FROM workflow_instances WHERE id = ?", (workflow_id,)
    ).fetchone()
    if workflow is None:
        return _result(False, 0, error={"seq": None, "code": "NO_WORKFLOW",
                                        "message": "Workflow does not exist."})

    rows = conn.execute(
        "SELECT * FROM state_transitions WHERE workflow_id = ? ORDER BY seq, id",
        (workflow_id,),
    ).fetchall()

    def fail(seq, code, message, state=None):
        return _result(False, len(rows), state,
                       {"seq": seq, "code": code, "message": message})

    previous_hash = GENESIS_HASH
    state = INITIAL_STATE
    evidence_round = 1
    policy = None            # {"reviewers": [ids], "required": k}, sealed by SUBMIT_EVIDENCE
    submitted_digest = None
    approvals, rejections = set(), set()
    vote_events = 0
    report_events = 0
    chain_mode = None        # "verification" | "review", sealed by SUBMIT_EVIDENCE
    reporters = set()

    for expected_seq, row in enumerate(rows, start=1):
        seq = row["seq"]
        if seq != expected_seq:
            return fail(seq, "SEQUENCE_GAP",
                        f"Expected link #{expected_seq} but found #{seq}: "
                        "a record was removed, reordered or inserted.", state.value)

        if row["prev_hash"] != previous_hash:
            return fail(seq, "BROKEN_LINK",
                        f"Link #{seq} does not point at the hash of link #{seq - 1}.",
                        state.value)

        if compute_hash(row) != row["hash"]:
            return fail(seq, "CONTENT_ALTERED",
                        f"Link #{seq} was modified after it was written "
                        "(its stored hash no longer matches its content).", state.value)

        try:
            from_state = WorkflowState(row["from_state"])
            event = WorkflowEvent(row["event"])
            to_state = WorkflowState(row["to_state"])
        except ValueError:
            return fail(seq, "UNKNOWN_SYMBOL",
                        f"Link #{seq} references a state or event the automaton does not have.",
                        state.value)

        if from_state != state:
            return fail(seq, "STATE_DISCONTINUITY",
                        f"Link #{seq} starts in {from_state.value} but the replayed "
                        f"state was {state.value}.", state.value)

        if TRANSITIONS.get(from_state, {}).get(event) != to_state:
            return fail(seq, "NOT_ACCEPTED_BY_AUTOMATON",
                        f"{from_state.value} --{event.value}--> {to_state.value} "
                        "is not an edge of the transition table.", state.value)

        if event == WorkflowEvent.SUBMIT_EVIDENCE and row["actor_role"] != "legacy":
            current = conn.execute(
                """
                SELECT id, evidence_type, evidence_url, title, description, created_at
                FROM evidence
                WHERE workflow_id = ? AND round = ?
                """,
                (workflow_id, evidence_round),
            ).fetchall()
            if evidence_digest(current) != row["evidence_digest"]:
                return fail(seq, "EVIDENCE_ALTERED",
                            "The evidence stored for this submission no longer matches "
                            "the digest that was sealed when it was submitted.",
                            state.value)

        problem = _check_mode_rules(conn, workflow, row, event, evidence_round,
                                    policy, chain_mode, reporters)
        if problem:
            return fail(seq, problem[0], problem[1], state.value)

        problem = _check_review_rules(conn, row, event, workflow_id, evidence_round,
                                      policy, approvals, rejections, submitted_digest)
        if problem:
            return fail(seq, problem[0], problem[1], state.value)

        if event == WorkflowEvent.SUBMIT_EVIDENCE:
            policy = _load_meta(row)
            submitted_digest = row["evidence_digest"]
            approvals, rejections, reporters = set(), set(), set()
            if row["actor_role"] != "legacy":
                chain_mode = (policy or {}).get("mode", "verification")
        elif event == WorkflowEvent.REASSIGN_REVIEWER and policy is not None:
            meta = _load_meta(row)
            policy["reviewers"] = [meta["add"] if r == meta["remove"] else r
                                   for r in policy["reviewers"]]
        elif event in (WorkflowEvent.VOTE_APPROVE, WorkflowEvent.VOTE_REJECT):
            vote_events += 1
            if policy is not None:
                (approvals if event == WorkflowEvent.VOTE_APPROVE else rejections).add(row["actor_id"])

        elif event == WorkflowEvent.SUBMIT_REPORT:
            report_events += 1
            reporters.add(row["actor_id"])

        if event in (WorkflowEvent.RESTART, WorkflowEvent.WITHDRAW, WorkflowEvent.NEW_VERSION):
            evidence_round += 1
            policy, submitted_digest = None, None
            approvals, rejections, reporters = set(), set(), set()

        previous_hash = row["hash"]
        state = to_state

    stored_decisions = conn.execute(
        "SELECT COUNT(*) FROM review_decisions WHERE workflow_id = ?", (workflow_id,)
    ).fetchone()[0]
    if stored_decisions != vote_events:
        return fail(None, "DECISION_MISMATCH",
                    f"{stored_decisions} reviewer decisions are stored but the chain records "
                    f"{vote_events} votes: a decision was added or removed outside the chain.",
                    state.value)

    stored_reports = conn.execute(
        "SELECT COUNT(*) FROM review_reports WHERE workflow_id = ?", (workflow_id,)
    ).fetchone()[0]
    if stored_reports != report_events:
        return fail(None, "REPORT_MISMATCH",
                    f"{stored_reports} reports are stored but the chain records {report_events}: "
                    "a report was added or removed outside the chain.", state.value)

    if (workflow["review_mode"] if "review_mode" in workflow.keys() else "verification") \
            != (chain_mode or workflow["review_mode"]):
        return fail(None, "MODE_MISMATCH",
                    "The stored mode of this request differs from the mode sealed in its history.",
                    state.value)

    from services.finding_service import verify_findings
    broken = verify_findings(conn, workflow_id)
    if broken:
        return fail(None, "FINDING_HISTORY_INVALID", broken[1], state.value)

    if len(rows) != workflow["transition_count"]:
        return fail(None, "COUNT_MISMATCH",
                    f"The workflow records {workflow['transition_count']} transitions "
                    f"but {len(rows)} exist: history was truncated or extended.", state.value)

    expected_head = previous_hash if rows else None
    if workflow["chain_head"] != expected_head:
        return fail(None, "HEAD_MISMATCH",
                    "The stored chain head does not match the last link.", state.value)

    if state.value != workflow["current_state"]:
        return fail(None, "STATE_MISMATCH",
                    f"Replaying the history ends in {state.value}, but the workflow is "
                    f"stored as {workflow['current_state']}.", state.value)

    return _result(True, len(rows), state.value, None, expected_head)
