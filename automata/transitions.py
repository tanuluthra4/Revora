"""The transition function (delta) of the verification automaton.

This module is *pure*: no database, no Flask. It is the single source of
truth for which moves are legal. The engine, routes, templates and the
state-diagram visualisation all read from `TRANSITIONS`.

Formally the machine is a deterministic finite automaton M = (Q, Sigma, delta, q0, F):
    Q      = the eight WorkflowState values
    Sigma  = the fourteen WorkflowEvent values (two are reviewer votes, three exist
             only for review-mode requests)
    delta  = TRANSITIONS (a *partial* function: undefined moves are rejected)
    q0     = CREATED
    F      = {VERIFIED}   (accepting state: the proof was approved)
"""
from .states import Actor, WorkflowEvent, WorkflowState

INITIAL_STATE = WorkflowState.CREATED
ACCEPTING_STATES = frozenset({WorkflowState.VERIFIED})

TRANSITIONS = {
    WorkflowState.CREATED: {
        WorkflowEvent.START: WorkflowState.IN_PROGRESS,
    },
    WorkflowState.IN_PROGRESS: {
        WorkflowEvent.REQUEST_EVIDENCE: WorkflowState.EVIDENCE_REQUIRED,
    },
    WorkflowState.EVIDENCE_REQUIRED: {
        WorkflowEvent.SUBMIT_EVIDENCE: WorkflowState.EVIDENCE_SUBMITTED,
    },
    WorkflowState.EVIDENCE_SUBMITTED: {
        WorkflowEvent.START_VALIDATION: WorkflowState.VALIDATING,
        WorkflowEvent.REASSIGN_REVIEWER: WorkflowState.EVIDENCE_SUBMITTED,
        WorkflowEvent.WITHDRAW: WorkflowState.IN_PROGRESS,
    },
    WorkflowState.VALIDATING: {
        # Votes are self-loops: each is recorded (and hash-chained) without
        # leaving VALIDATING. APPROVE / REJECT are then fired by the engine the
        # moment the votes decide the outcome; no person can fire them.
        WorkflowEvent.VOTE_APPROVE: WorkflowState.VALIDATING,
        WorkflowEvent.VOTE_REJECT: WorkflowState.VALIDATING,
        WorkflowEvent.REASSIGN_REVIEWER: WorkflowState.VALIDATING,
        WorkflowEvent.WITHDRAW: WorkflowState.IN_PROGRESS,
        WorkflowEvent.APPROVE: WorkflowState.VERIFIED,
        WorkflowEvent.REJECT: WorkflowState.REJECTED,
        # Review mode: each report is a chained self-loop; the engine fires
        # COMPLETE_REVIEW when the k-th report arrives.
        WorkflowEvent.SUBMIT_REPORT: WorkflowState.VALIDATING,
        WorkflowEvent.COMPLETE_REVIEW: WorkflowState.REVIEWED,
    },
    # REVIEWED is not terminal, but only the owner's NEW_VERSION leaves it.
    WorkflowState.REVIEWED: {
        WorkflowEvent.NEW_VERSION: WorkflowState.IN_PROGRESS,
    },
    # VERIFIED is terminal: a verified proof can never be silently re-opened.
    WorkflowState.REJECTED: {
        WorkflowEvent.RESTART: WorkflowState.IN_PROGRESS,
    },
}

# Who may fire each event. The owner drives the work; only reviewers assigned
# to the submission (never its owner) may open a review and vote. The final
# decision is SYSTEM: nobody can declare a task verified; it follows from the
# votes and the sealed approval policy.
REQUIRED_ACTOR = {
    WorkflowEvent.START: Actor.OWNER,
    WorkflowEvent.REQUEST_EVIDENCE: Actor.OWNER,
    WorkflowEvent.SUBMIT_EVIDENCE: Actor.OWNER,
    WorkflowEvent.START_VALIDATION: Actor.REVIEWER,
    WorkflowEvent.VOTE_APPROVE: Actor.REVIEWER,
    WorkflowEvent.VOTE_REJECT: Actor.REVIEWER,
    WorkflowEvent.APPROVE: Actor.SYSTEM,
    WorkflowEvent.REJECT: Actor.SYSTEM,
    WorkflowEvent.REASSIGN_REVIEWER: Actor.OWNER,
    WorkflowEvent.WITHDRAW: Actor.OWNER,
    WorkflowEvent.RESTART: Actor.OWNER,
    WorkflowEvent.SUBMIT_REPORT: Actor.REVIEWER,
    WorkflowEvent.COMPLETE_REVIEW: Actor.SYSTEM,
    WorkflowEvent.NEW_VERSION: Actor.OWNER,
}


class InvalidTransitionError(ValueError):
    """Raised when delta(state, event) is undefined."""


def get_next_state(current_state: WorkflowState, event: WorkflowEvent) -> WorkflowState:
    """Return delta(current_state, event) or raise InvalidTransitionError."""
    try:
        return TRANSITIONS[current_state][event]
    except KeyError:
        raise InvalidTransitionError(
            f"No transition defined for state {current_state.value} "
            f"with event {event.value}."
        ) from None


def get_allowed_events(current_state: WorkflowState) -> list:
    """Events that are defined (by the table alone) from `current_state`."""
    return list(TRANSITIONS.get(current_state, {}).keys())


def is_terminal(state: WorkflowState) -> bool:
    return not TRANSITIONS.get(state)


def events_available_to(state: WorkflowState, is_owner: bool, is_reviewer: bool) -> list:
    """Events defined from `state` that this viewer is also allowed to fire."""
    available = []
    for event in get_allowed_events(state):
        required = REQUIRED_ACTOR[event]
        if required is Actor.OWNER and is_owner:
            available.append(event)
        elif required is Actor.REVIEWER and is_reviewer and not is_owner:
            available.append(event)
    return available


def all_transitions():
    """Yield every (from_state, event, to_state) edge of the automaton."""
    for from_state, moves in TRANSITIONS.items():
        for event, to_state in moves.items():
            yield from_state, event, to_state
