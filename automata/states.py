from enum import Enum


class WorkflowState(Enum):
    """The states (Q) of the verification automaton."""
    CREATED = "CREATED"
    IN_PROGRESS = "IN_PROGRESS"
    EVIDENCE_REQUIRED = "EVIDENCE_REQUIRED"
    EVIDENCE_SUBMITTED = "EVIDENCE_SUBMITTED"
    VALIDATING = "VALIDATING"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"
    # Review mode only: the required number of reports has been submitted. This
    # records that the REVIEW is complete. It is never a statement that the
    # project is ready, approved or verified. Not terminal: NEW_VERSION leaves it.
    REVIEWED = "REVIEWED"


class WorkflowEvent(Enum):
    """The input alphabet (Sigma) of the verification automaton."""
    START = "START"
    REQUEST_EVIDENCE = "REQUEST_EVIDENCE"
    SUBMIT_EVIDENCE = "SUBMIT_EVIDENCE"
    START_VALIDATION = "START_VALIDATION"
    VOTE_APPROVE = "VOTE_APPROVE"            # one reviewer approves (state unchanged)
    VOTE_REJECT = "VOTE_REJECT"              # one reviewer rejects (state unchanged)
    APPROVE = "APPROVE"                      # fired by the engine when the quorum is met
    REJECT = "REJECT"                        # fired by the engine when approval is impossible
    REASSIGN_REVIEWER = "REASSIGN_REVIEWER"  # owner swaps a reviewer who has not voted
    WITHDRAW = "WITHDRAW"                    # owner withdraws a submission to change evidence
    RESTART = "RESTART"
    # Review mode only (see services/automata_engine.py, MODE_EVENTS).
    SUBMIT_REPORT = "SUBMIT_REPORT"          # one reviewer's structured report (self-loop)
    COMPLETE_REVIEW = "COMPLETE_REVIEW"      # fired by the engine when enough reports exist
    NEW_VERSION = "NEW_VERSION"              # owner opens the next version after a review


class Actor(Enum):
    """Who is allowed to fire an event."""
    OWNER = "OWNER"        # the developer who owns the task
    REVIEWER = "REVIEWER"  # a reviewer assigned to this submission (never its owner)
    SYSTEM = "SYSTEM"      # fired only by the engine itself, from the votes
