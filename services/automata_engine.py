"""Database-backed workflow engine.

Responsibilities, in the order a request is checked:

  1. the event exists                      -> UNKNOWN_EVENT
  2. delta(state, event) is defined        -> INVALID_TRANSITION   (automata/)
  3. the actor is allowed to fire it       -> UNAUTHORIZED_ACTOR
  4. business guards pass                  -> GUARD_FAILED / EVIDENCE_CHANGED
  5. state + hash-chained history + side effects are written in ONE
     transaction (BEGIN IMMEDIATE, compare-and-swap on the old state).

Multi-reviewer decisions
------------------------
Each reviewer's vote is its own chained transition (a self-loop in VALIDATING)
plus an immutable `review_decisions` row. The moment the votes decide the
outcome the engine appends the resolving APPROVE or REJECT transition in the
same transaction. Nobody can fire APPROVE/REJECT directly.

Approval policy (k of n, sealed into the chain at submission):
  * VERIFIED  when k reviewers approved;
  * REJECTED  when approval became impossible (more than n-k rejections).
    With k = n (every reviewer required, e.g. the default 2 of 2) this is
    exactly "a single rejection rejects the submission".

Refused attempts (steps 1-4) are recorded in `blocked_actions`, outside the
rolled-back transaction, so they are visible in the UI.
"""
import json
import sqlite3
from datetime import datetime, timezone

from automata.states import Actor, WorkflowEvent, WorkflowState
from automata.transitions import REQUIRED_ACTOR, get_allowed_events
from automata.verification_machine import VerificationMachine
from database.db import get_db_connection, transaction
from services.integrity import (GENESIS_HASH, REVIEW_ONLY_EVENTS, VERIFICATION_ONLY_EVENTS,
                                compute_hash, evidence_digest, report_digest, verify_chain,
                                version_digest)
from services.report_service import ReportInvalid, validate_report

MIN_REASON_LENGTH = 5
MAX_NOTE = 2000
VOTES = (WorkflowEvent.VOTE_APPROVE, WorkflowEvent.VOTE_REJECT)


class TransitionBlocked(ValueError):
    """A requested transition was refused. `code` says why."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class WorkflowNotFound(LookupError):
    pass


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class AutomataEngine:

    # ------------------------------------------------------------------ create
    def create_workflow(self, user_id, task_id=None, conn=None):
        """Create the workflow for a task. Idempotent: a task has at most one.

        Pass `conn` to join the caller's transaction (e.g. task creation).
        """
        if conn is None:
            with transaction() as own:
                return self.create_workflow(user_id, task_id, conn=own)

        if task_id is not None:
            existing = conn.execute(
                "SELECT id FROM workflow_instances WHERE task_id = ?", (task_id,)
            ).fetchone()
            if existing:
                return existing["id"]

        cursor = conn.execute(
            "INSERT INTO workflow_instances (user_id, task_id, current_state) VALUES (?, ?, ?)",
            (user_id, task_id, WorkflowState.CREATED.value),
        )
        return cursor.lastrowid

    # -------------------------------------------------------------------- read
    def get_workflow(self, workflow_id):
        conn = get_db_connection()
        try:
            return conn.execute(
                "SELECT * FROM workflow_instances WHERE id = ?", (workflow_id,)
            ).fetchone()
        finally:
            conn.close()

    def get_workflow_for_task(self, task_id):
        conn = get_db_connection()
        try:
            return conn.execute(
                "SELECT * FROM workflow_instances WHERE task_id = ?", (task_id,)
            ).fetchone()
        finally:
            conn.close()

    def get_history(self, workflow_id):
        conn = get_db_connection()
        try:
            return conn.execute(
                """
                SELECT t.seq, t.from_state, t.event, t.to_state, t.condition_result,
                       t.reason, t.actor_id, t.actor_role, t.evidence_digest,
                       t.prev_hash, t.hash, t.meta, t.created_at, u.username AS actor_name
                FROM state_transitions t
                LEFT JOIN users u ON u.id = t.actor_id
                WHERE t.workflow_id = ?
                ORDER BY t.seq ASC, t.id ASC
                """,
                (workflow_id,),
            ).fetchall()
        finally:
            conn.close()

    def get_blocked_actions(self, workflow_id, limit=10):
        conn = get_db_connection()
        try:
            return conn.execute(
                """
                SELECT b.event, b.from_state, b.code, b.message, b.created_at,
                       u.username AS actor_name
                FROM blocked_actions b
                LEFT JOIN users u ON u.id = b.actor_id
                WHERE b.workflow_id = ?
                ORDER BY b.id DESC LIMIT ?
                """,
                (workflow_id, limit),
            ).fetchall()
        finally:
            conn.close()

    def verify_history(self, workflow_id):
        conn = get_db_connection()
        try:
            return verify_chain(conn, workflow_id)
        finally:
            conn.close()

    # -------------------------------------------------------------- transition
    def transition(self, workflow_id, event, actor_id, reason=None, extra=None):
        """Fire `event` on a workflow as `actor_id`.

        `extra` carries event-specific input: for votes {"private_notes", "note_public"};
        for REASSIGN_REVIEWER {"remove", "add"} (username or e-mail).

        Returns {"from_state", "event", "to_state", "seq", "hash"}.
        Raises TransitionBlocked (a ValueError) when refused, WorkflowNotFound
        when the workflow does not exist. On any failure nothing is written.
        """
        reason = (reason or "").strip() or None
        extra = extra or {}
        conn = get_db_connection()
        event_name = event.value if isinstance(event, WorkflowEvent) else str(event)
        try:
            try:
                with transaction(conn):
                    return self._apply(conn, workflow_id, event, actor_id, reason, extra)
            except TransitionBlocked as blocked:
                from_state = self._peek_state(conn, workflow_id)
                self._log_blocked(conn, workflow_id, actor_id, event_name, from_state, blocked)
                raise
        finally:
            conn.close()

    @staticmethod
    def _peek_state(conn, workflow_id):
        row = conn.execute(
            "SELECT current_state FROM workflow_instances WHERE id = ?", (workflow_id,)
        ).fetchone()
        return row["current_state"] if row else None

    @staticmethod
    def _log_blocked(conn, workflow_id, actor_id, event_name, from_state, blocked):
        try:
            with transaction(conn):
                conn.execute(
                    """
                    INSERT INTO blocked_actions
                        (workflow_id, actor_id, event, from_state, code, message)
                    SELECT ?, ?, ?, ?, ?, ?
                    WHERE EXISTS (SELECT 1 FROM workflow_instances WHERE id = ?)
                    """,
                    (workflow_id, actor_id, event_name, from_state, blocked.code,
                     str(blocked), workflow_id),
                )
        except sqlite3.Error:
            # Logging a refusal must never mask the refusal itself.
            pass

    # ------------------------------------------------------------ assignments
    @staticmethod
    def active_reviewers(conn, workflow_id, round_no):
        """Ids of reviewers currently assigned (not cancelled) for this round."""
        return [r["reviewer_id"] for r in conn.execute(
            "SELECT reviewer_id FROM review_assignments "
            "WHERE workflow_id = ? AND round = ? AND status = 'assigned' ORDER BY reviewer_id",
            (workflow_id, round_no),
        )]

    @staticmethod
    def _decisions(conn, workflow_id, round_no):
        return conn.execute(
            "SELECT reviewer_id, decision FROM review_decisions WHERE workflow_id = ? AND round = ?",
            (workflow_id, round_no),
        ).fetchall()

    @staticmethod
    def _find_reviewer(conn, identifier):
        identifier = (identifier or "").strip().lower()
        if not identifier:
            return None
        return conn.execute(
            "SELECT id, username FROM users WHERE role = 'reviewer' "
            "AND (lower(username) = ? OR lower(email) = ?)",
            (identifier, identifier),
        ).fetchone()

    # ------------------------------------------------------------------- apply
    def _apply(self, conn, workflow_id, event, actor_id, reason, extra):
        workflow = conn.execute(
            "SELECT * FROM workflow_instances WHERE id = ?", (workflow_id,)
        ).fetchone()
        if workflow is None:
            raise WorkflowNotFound("Workflow not found.")

        # 1. known event
        if not isinstance(event, WorkflowEvent):
            try:
                event = WorkflowEvent(str(event))
            except ValueError:
                raise TransitionBlocked(
                    "UNKNOWN_EVENT", f"'{event}' is not an event of this automaton."
                ) from None

        try:
            state = WorkflowState(workflow["current_state"])
        except ValueError:
            raise TransitionBlocked(
                "CORRUPT_STATE",
                f"Workflow has an unknown stored state '{workflow['current_state']}'.",
            ) from None

        # 2. is delta(state, event) defined?
        machine = VerificationMachine(state)
        if not machine.can_transition(event):
            allowed = ", ".join(e.value for e in get_allowed_events(state)) or "none (terminal state)"
            raise TransitionBlocked(
                "INVALID_TRANSITION",
                f"{event.value} is not allowed in state {state.value}. "
                f"Events accepted here: {allowed}.",
            )

        # 2b. mode guard: some events exist only for one kind of request
        mode = workflow["review_mode"]
        if (mode == "review" and event in VERIFICATION_ONLY_EVENTS) or \
                (mode != "review" and event in REVIEW_ONLY_EVENTS):
            raise TransitionBlocked(
                "INVALID_TRANSITION",
                f"{event.value} is not available for a "
                f"{'review' if mode == 'review' else 'verification'} request.")

        # 3. is this actor allowed to fire it? (role read from the DB, never trusted from the client)
        actor = conn.execute("SELECT id, role FROM users WHERE id = ?", (actor_id,)).fetchone()
        if actor is None:
            raise TransitionBlocked("UNAUTHORIZED_ACTOR", "Unknown user.")
        assigned = self.active_reviewers(conn, workflow_id, workflow["round"])
        self._check_actor(event, workflow, actor, assigned)

        # 4. business guards
        digest, meta = None, None
        if event == WorkflowEvent.SUBMIT_EVIDENCE:
            digest, meta, reason = self._guard_submit(conn, workflow, assigned, reason)
        elif event in VOTES:
            digest = self._guard_vote(conn, workflow, actor, reason, extra)
        elif event == WorkflowEvent.REASSIGN_REVIEWER:
            meta, reason = self._guard_reassign(conn, workflow, assigned, reason, extra)
        elif event == WorkflowEvent.SUBMIT_REPORT:
            meta, reason = self._guard_report(conn, workflow, actor, extra)

        step = self._append(conn, workflow, event, actor, reason, digest, meta, extra)

        # Quorum: a vote may decide the outcome. Resolve in the same transaction.
        if event in VOTES:
            self._resolve_if_decided(conn, workflow_id, actor)
            step["to_state"] = self._peek_state(conn, workflow_id)
        elif event == WorkflowEvent.SUBMIT_REPORT:
            self._complete_if_enough_reports(conn, workflow_id, actor)
            step["to_state"] = self._peek_state(conn, workflow_id)
        return step

    # ----------------------------------------------------------------- guards
    @staticmethod
    def _round_evidence(conn, workflow):
        return conn.execute(
            """
            SELECT id, evidence_type, evidence_url, title, description, created_at
            FROM evidence WHERE workflow_id = ? AND round = ?
            """,
            (workflow["id"], workflow["round"]),
        ).fetchall()

    @staticmethod
    def _version_row(conn, workflow):
        return conn.execute(
            "SELECT * FROM submission_versions WHERE workflow_id = ? AND round = ?",
            (workflow["id"], workflow["round"])).fetchone()

    @staticmethod
    def _version_digest_of(version):
        return version_digest(version["project_id"], version["repo_url"], version["commit_sha"],
                              version["demo_url"], version["tech_stack"], version["review_goal"],
                              version["questions"])

    def _guard_submit(self, conn, workflow, assigned, reason):
        review = workflow["review_mode"] == "review"
        evidence = self._round_evidence(conn, workflow)
        version = None
        if review:
            version = self._version_row(conn, workflow)
            if version is None or not (version["repo_url"] and version["commit_sha"]
                                       and version["tech_stack"]):
                raise TransitionBlocked(
                    "GUARD_FAILED",
                    "Describe this version (repository, commit, tech stack, review goal) "
                    "before submitting it for review.")
            owns = conn.execute("SELECT 1 FROM projects WHERE id = ? AND user_id = ?",
                                (version["project_id"], workflow["user_id"])).fetchone()
            if owns is None:
                raise TransitionBlocked("GUARD_FAILED", "The version must belong to one of your projects.")
        elif not evidence:
            raise TransitionBlocked(
                "GUARD_FAILED", "Add at least one piece of evidence before submitting for review."
            )
        required = workflow["required_approvals"]
        if not assigned or not (1 <= required <= len(assigned)):
            raise TransitionBlocked(
                "GUARD_FAILED",
                "Choose the reviewers who should " + ("review" if review else "verify") +
                " this " + ("version" if review else "evidence") + ", and how many must " +
                ("report" if review else "approve") + ", before submitting.",
            )
        if workflow["user_id"] in assigned:
            raise TransitionBlocked("GUARD_FAILED", "You cannot review your own work.")
        names = [r["username"] for r in conn.execute(
            f"SELECT username FROM users WHERE id IN ({','.join('?' * len(assigned))}) "
            "ORDER BY username", assigned)]
        policy = {"reviewers": assigned, "required": required}
        if review:
            policy.update({"mode": "review", "goal": version["review_goal"],
                           "version_digest": self._version_digest_of(version)})
            noun = "report" if required == 1 else "reports"
            reason = (f"Version submitted to {', '.join(names)}; {required} of {len(assigned)} "
                      f"{noun} required" + (f". {reason}" if reason else ""))
        else:
            reason = (f"Sent to {', '.join(names)}; {required} of {len(assigned)} approval"
                      f"{'' if required == 1 else 's'} required" + (f". {reason}" if reason else ""))
        # The reviewer policy (and, for review requests, the version) is sealed into the history.
        meta = json.dumps(policy, sort_keys=True, separators=(",", ":"))
        return evidence_digest(evidence), meta, reason

    def _guard_report(self, conn, workflow, actor, extra):
        """Validate one reviewer's report. Consent is checked first; on any failure nothing is stored."""
        sealed = conn.execute(
            "SELECT meta FROM state_transitions WHERE workflow_id = ? AND event = 'SUBMIT_EVIDENCE' "
            "ORDER BY seq DESC LIMIT 1", (workflow["id"],)).fetchone()
        policy = json.loads(sealed["meta"]) if sealed and sealed["meta"] else {}
        goal = policy.get("goal")
        try:
            data = validate_report(conn, workflow, goal if goal in ("production", "interview") else "production",
                                   extra)
        except ReportInvalid as exc:
            raise TransitionBlocked("GUARD_FAILED", " ".join(exc.problems)) from None
        if conn.execute("SELECT 1 FROM review_reports WHERE workflow_id = ? AND round = ? "
                        "AND reviewer_id = ?", (workflow["id"], workflow["round"], actor["id"])).fetchone():
            raise TransitionBlocked("GUARD_FAILED", "You have already submitted a report for this version.")
        version = self._version_row(conn, workflow)
        if version is None or self._version_digest_of(version) != policy.get("version_digest"):
            raise TransitionBlocked("EVIDENCE_CHANGED",
                                    "The version no longer matches what was submitted for review.")
        from services.report_service import CONSENT_VERSION
        digest = report_digest(workflow["round"], actor["id"], goal, CONSENT_VERSION,
                               data["report"], data["findings"])
        extra["_report"] = {**data, "digest": digest, "goal": goal}
        meta = json.dumps({"report_digest": digest, "consent_version": CONSENT_VERSION,
                           "findings": len(data["findings"])}, sort_keys=True, separators=(",", ":"))
        n = len(data["findings"])
        return meta, f"Report submitted with {n} finding{'' if n == 1 else 's'}."

    def _guard_vote(self, conn, workflow, actor, reason, extra):
        if not reason or len(reason) < MIN_REASON_LENGTH:
            raise TransitionBlocked(
                "GUARD_FAILED",
                f"A note of at least {MIN_REASON_LENGTH} characters is required with every decision.",
            )
        if len(reason) > MAX_NOTE or len(extra.get("private_notes") or "") > MAX_NOTE:
            raise TransitionBlocked("GUARD_FAILED", f"Notes can be at most {MAX_NOTE} characters.")
        if any(d["reviewer_id"] == actor["id"] for d in
               self._decisions(conn, workflow["id"], workflow["round"])):
            raise TransitionBlocked("GUARD_FAILED", "You have already decided on this submission.")

        # Every reviewer must judge the same, unchanged evidence version.
        sealed = conn.execute(
            "SELECT evidence_digest FROM state_transitions WHERE workflow_id = ? "
            "AND event = 'SUBMIT_EVIDENCE' ORDER BY seq DESC LIMIT 1",
            (workflow["id"],),
        ).fetchone()
        sealed_digest = sealed["evidence_digest"] if sealed else None
        if sealed_digest is not None and \
                evidence_digest(self._round_evidence(conn, workflow)) != sealed_digest:
            raise TransitionBlocked(
                "EVIDENCE_CHANGED",
                "The evidence no longer matches the version that was submitted for review. "
                "The submission must be withdrawn and submitted again.",
            )
        return sealed_digest

    def _guard_reassign(self, conn, workflow, assigned, reason, extra):
        old = self._find_reviewer(conn, extra.get("remove"))
        new = self._find_reviewer(conn, extra.get("add"))
        if old is None or old["id"] not in assigned:
            raise TransitionBlocked("GUARD_FAILED", "The reviewer to replace is not assigned to this submission.")
        if any(d["reviewer_id"] == old["id"] for d in self._decisions(conn, workflow["id"], workflow["round"])):
            raise TransitionBlocked(
                "GUARD_FAILED", "A reviewer who has already decided cannot be replaced: "
                "their decision stays part of the record.")
        if conn.execute("SELECT 1 FROM review_reports WHERE workflow_id = ? AND round = ? "
                        "AND reviewer_id = ?", (workflow["id"], workflow["round"], old["id"])).fetchone():
            raise TransitionBlocked(
                "GUARD_FAILED", "A reviewer who has already submitted a report cannot be replaced: "
                "their report stays part of the record.")
        if new is None:
            raise TransitionBlocked("GUARD_FAILED", "No reviewer account matches the replacement.")
        if new["id"] == workflow["user_id"]:
            raise TransitionBlocked("GUARD_FAILED", "You cannot review your own work.")
        if new["id"] in assigned:
            raise TransitionBlocked("GUARD_FAILED", "That reviewer is already assigned.")
        meta = json.dumps({"remove": old["id"], "add": new["id"]}, sort_keys=True, separators=(",", ":"))
        reason = f"Reviewer {old['username']} replaced by {new['username']}" + (f". {reason}" if reason else "")
        return meta, reason

    # -------------------------------------------------------------- actor rule
    @staticmethod
    def _check_actor(event, workflow, actor, assigned):
        required = REQUIRED_ACTOR[event]
        is_owner = actor["id"] == workflow["user_id"]
        if required is Actor.SYSTEM:
            raise TransitionBlocked(
                "UNAUTHORIZED_ACTOR",
                f"{event.value} cannot be fired by a person: the outcome follows automatically "
                "from the reviewers' decisions and the approval policy.",
            )
        if required is Actor.OWNER and not is_owner:
            raise TransitionBlocked(
                "UNAUTHORIZED_ACTOR", f"Only the task owner can fire {event.value}."
            )
        if required is Actor.REVIEWER:
            if is_owner:
                raise TransitionBlocked(
                    "UNAUTHORIZED_ACTOR",
                    "You cannot review your own work: different reviewers must decide.",
                )
            if actor["role"] != "reviewer":
                raise TransitionBlocked(
                    "UNAUTHORIZED_ACTOR", f"Only a reviewer can fire {event.value}."
                )
            if actor["id"] not in assigned:
                raise TransitionBlocked(
                    "UNAUTHORIZED_ACTOR",
                    "This evidence was not sent to you: only the reviewers the developer "
                    "chose can act on it.",
                )

    # ----------------------------------------------------------------- quorum
    def _resolve_if_decided(self, conn, workflow_id, actor):
        workflow = conn.execute(
            "SELECT * FROM workflow_instances WHERE id = ?", (workflow_id,)
        ).fetchone()
        decisions = self._decisions(conn, workflow_id, workflow["round"])
        approvals = sum(1 for d in decisions if d["decision"] == "approve")
        rejections = sum(1 for d in decisions if d["decision"] == "reject")
        total = len(self.active_reviewers(conn, workflow_id, workflow["round"]))
        required = workflow["required_approvals"]

        if approvals >= required:
            self._append(conn, workflow, WorkflowEvent.APPROVE, actor,
                         f"Approval policy met: {approvals} of {total} reviewers approved "
                         f"({required} required).", None, None, {})
        elif rejections > total - required:
            self._append(conn, workflow, WorkflowEvent.REJECT, actor,
                         f"Approval no longer possible: {rejections} of {total} reviewers rejected "
                         f"({required} approvals were required).", None, None, {})

    def _complete_if_enough_reports(self, conn, workflow_id, actor):
        """Quorum of REPORTS, not approval: it records that the review is complete and
        says nothing about whether the project is ready."""
        workflow = conn.execute("SELECT * FROM workflow_instances WHERE id = ?",
                                (workflow_id,)).fetchone()
        reports = conn.execute("SELECT COUNT(*) FROM review_reports WHERE workflow_id = ? AND round = ?",
                               (workflow_id, workflow["round"])).fetchone()[0]
        total = len(self.active_reviewers(conn, workflow_id, workflow["round"]))
        required = workflow["required_approvals"]
        if reports >= required:
            self._append(conn, workflow, WorkflowEvent.COMPLETE_REVIEW, actor,
                         f"Review complete: {reports} of {total} reports submitted "
                         f"({required} required). This is not a readiness verdict.",
                         None, None, {})

    # ----------------------------------------------------------------- append
    def _append(self, conn, workflow, event, actor, reason, digest, meta, extra):
        """Write one transition: history row, state update (compare-and-swap), side effects."""
        machine = VerificationMachine(WorkflowState(workflow["current_state"]))
        step = machine.transition(event)          # pure; raises if delta is undefined
        seq = workflow["transition_count"] + 1
        required = REQUIRED_ACTOR[event]
        record = {
            "workflow_id": workflow["id"],
            "seq": seq,
            "from_state": step["from_state"],
            "event": step["event"],
            "to_state": step["to_state"],
            "condition_result": "PASS",
            "reason": reason,
            "actor_id": actor["id"],
            "actor_role": ("owner" if required is Actor.OWNER
                           else "system" if required is Actor.SYSTEM else actor["role"]),
            "evidence_digest": digest,
            "meta": meta,
            "created_at": _now(),
            "prev_hash": workflow["chain_head"] or GENESIS_HASH,
        }
        record["hash"] = compute_hash(record)

        conn.execute(
            """
            INSERT INTO state_transitions
                (workflow_id, seq, from_state, event, to_state, condition_result, reason,
                 actor_id, actor_role, evidence_digest, meta, created_at, prev_hash, hash)
            VALUES (:workflow_id, :seq, :from_state, :event, :to_state, :condition_result,
                    :reason, :actor_id, :actor_role, :evidence_digest, :meta, :created_at,
                    :prev_hash, :hash)
            """,
            record,
        )

        new_state = step["to_state"]
        is_decision = event in (WorkflowEvent.APPROVE, WorkflowEvent.REJECT)
        new_round = event in (WorkflowEvent.RESTART, WorkflowEvent.WITHDRAW, WorkflowEvent.NEW_VERSION)
        cursor = conn.execute(
            """
            UPDATE workflow_instances
            SET current_state = ?, result = ?, completed_at = ?, round = round + ?,
                chain_head = ?, transition_count = ?
            WHERE id = ? AND current_state = ? AND transition_count = ?
            """,
            (
                new_state,
                new_state if is_decision else (None if new_round else workflow["result"]),
                _now() if is_decision else (None if new_round else workflow["completed_at"]),
                1 if new_round else 0,
                record["hash"],
                seq,
                workflow["id"],
                workflow["current_state"],
                workflow["transition_count"],
            ),
        )
        if cursor.rowcount != 1:
            raise TransitionBlocked(
                "CONFLICT", "The workflow changed while this action was being processed. Try again."
            )

        self._side_effects(conn, workflow, event, actor, reason, digest, meta, extra)
        return {
            "from_state": step["from_state"],
            "event": step["event"],
            "to_state": new_state,
            "seq": seq,
            "hash": record["hash"],
        }

    # ----------------------------------------------------------- side effects
    @staticmethod
    def _carry_version_forward(conn, wid, round_no):
        """The next round starts from an editable copy of the previous version's description."""
        conn.execute(
            """
            INSERT OR IGNORE INTO submission_versions
                (workflow_id, round, project_id, review_goal, repo_url, commit_sha, demo_url,
                 tech_stack, questions)
            SELECT workflow_id, round + 1, project_id, review_goal, repo_url, commit_sha, demo_url,
                   tech_stack, questions
            FROM submission_versions WHERE workflow_id = ? AND round = ?
            """, (wid, round_no))

    @staticmethod
    def _side_effects(conn, workflow, event, actor, reason, digest, meta, extra):
        wid, round_no = workflow["id"], workflow["round"]

        if event == WorkflowEvent.SUBMIT_EVIDENCE:
            conn.execute(
                "UPDATE evidence SET verification_status = 'submitted' "
                "WHERE workflow_id = ? AND round = ?", (wid, round_no))
            if workflow["review_mode"] == "review":
                conn.execute("UPDATE submission_versions SET locked_at = ? "
                             "WHERE workflow_id = ? AND round = ?", (_now(), wid, round_no))

        elif event == WorkflowEvent.SUBMIT_REPORT:
            rep = extra["_report"]
            cursor = conn.execute(
                """
                INSERT INTO review_reports
                    (workflow_id, round, reviewer_id, review_goal, summary, strengths, unresolved,
                     assessed, not_assessed, interview_qs, private_notes, consent_ack_at,
                     consent_version, content_digest, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (wid, round_no, actor["id"], rep["goal"], rep["report"]["summary"],
                 rep["report"]["strengths"], rep["report"]["unresolved"], rep["report"]["assessed"],
                 rep["report"]["not_assessed"], rep["report"]["interview_qs"], rep["private_notes"],
                 _now(), json.loads(meta)["consent_version"], rep["digest"], _now()))
            for f in rep["findings"]:
                conn.execute(
                    """
                    INSERT INTO review_findings
                        (report_id, seq, severity, category, title, explanation, suggested_fix,
                         reasoning, file_path, commit_ref, repro_evidence, prior_finding_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (cursor.lastrowid, f["seq"], f["severity"], f["category"], f["title"],
                     f["explanation"], f["suggested_fix"], f["reasoning"], f["file_path"],
                     f["commit_ref"], f["repro_evidence"], f["prior_finding_id"]))

        elif event == WorkflowEvent.NEW_VERSION:
            conn.execute(
                "INSERT OR IGNORE INTO review_assignments (workflow_id, round, reviewer_id) "
                "SELECT workflow_id, round + 1, reviewer_id FROM review_assignments "
                "WHERE workflow_id = ? AND round = ? AND status = 'assigned'", (wid, round_no))
            AutomataEngine._carry_version_forward(conn, wid, round_no)

        elif event == WorkflowEvent.COMPLETE_REVIEW:
            pass      # the state change IS the effect; the task is never marked completed

        elif event in VOTES:
            decision = "approve" if event == WorkflowEvent.VOTE_APPROVE else "reject"
            conn.execute(
                """
                INSERT INTO review_decisions
                    (workflow_id, round, reviewer_id, decision, notes, private_notes,
                     note_public, evidence_digest, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (wid, round_no, actor["id"], decision, reason,
                 (extra.get("private_notes") or "").strip() or None,
                 1 if extra.get("note_public") else 0, digest, _now()),
            )
            conn.execute(
                """
                INSERT INTO verifications
                    (evidence_id, status, verified_by, verification_method, verified_at, comments)
                SELECT id, ?, ?, 'manual_review', CURRENT_TIMESTAMP, ?
                FROM evidence WHERE workflow_id = ? AND round = ?
                """,
                ("approved" if decision == "approve" else "rejected", actor["id"], reason, wid, round_no),
            )

        elif event in (WorkflowEvent.APPROVE, WorkflowEvent.REJECT):
            approved = event == WorkflowEvent.APPROVE
            conn.execute(
                "UPDATE evidence SET verification_status = ? WHERE workflow_id = ? AND round = ?",
                ("approved" if approved else "rejected", wid, round_no))
            if approved and workflow["task_id"] is not None:
                conn.execute(
                    "UPDATE tasks SET status = 'Completed', "
                    "completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP) WHERE id = ?",
                    (workflow["task_id"],))

        elif event == WorkflowEvent.REASSIGN_REVIEWER:
            change = json.loads(meta)
            conn.execute(
                "UPDATE review_assignments SET status = 'cancelled', cancelled_at = ? "
                "WHERE workflow_id = ? AND round = ? AND reviewer_id = ?",
                (_now(), wid, round_no, change["remove"]))
            conn.execute(
                """
                INSERT INTO review_assignments (workflow_id, round, reviewer_id)
                VALUES (?, ?, ?)
                ON CONFLICT (workflow_id, round, reviewer_id)
                DO UPDATE SET status = 'assigned', cancelled_at = NULL, assigned_at = ?
                """,
                (wid, round_no, change["add"], _now()))

        elif event in (WorkflowEvent.RESTART, WorkflowEvent.WITHDRAW):
            # The next round starts with the same reviewers; the owner may change them.
            conn.execute(
                "INSERT OR IGNORE INTO review_assignments (workflow_id, round, reviewer_id) "
                "SELECT workflow_id, round + 1, reviewer_id FROM review_assignments "
                "WHERE workflow_id = ? AND round = ? AND status = 'assigned'", (wid, round_no))
            if event == WorkflowEvent.WITHDRAW:
                # Old assignments of the superseded round are cancelled (kept, not deleted);
                # evidence is copied forward as editable drafts for the new round.
                conn.execute(
                    "UPDATE review_assignments SET status = 'cancelled', cancelled_at = ? "
                    "WHERE workflow_id = ? AND round = ? AND status = 'assigned'",
                    (_now(), wid, round_no))
                conn.execute(
                    "UPDATE evidence SET verification_status = 'withdrawn' "
                    "WHERE workflow_id = ? AND round = ?", (wid, round_no))
                if workflow["review_mode"] == "review":
                    AutomataEngine._carry_version_forward(conn, wid, round_no)
                conn.execute(
                    """
                    INSERT INTO evidence
                        (user_id, project_id, task_id, workflow_id, round, title, description,
                         evidence_type, evidence_url, verification_status)
                    SELECT user_id, project_id, task_id, workflow_id, round + 1, title,
                           description, evidence_type, evidence_url, 'draft'
                    FROM evidence WHERE workflow_id = ? AND round = ?
                    """,
                    (wid, round_no))
