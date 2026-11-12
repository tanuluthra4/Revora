"""Pure automaton tests: no database, no Flask."""
import itertools

import pytest

from automata.states import Actor, WorkflowEvent as E, WorkflowState as S
from automata.transitions import (
    ACCEPTING_STATES, INITIAL_STATE, REQUIRED_ACTOR, TRANSITIONS, InvalidTransitionError,
    all_transitions, events_available_to, get_next_state, is_terminal,
)
from automata.verification_machine import VerificationMachine

ALLOWED = [
    (S.CREATED, E.START, S.IN_PROGRESS),
    (S.IN_PROGRESS, E.REQUEST_EVIDENCE, S.EVIDENCE_REQUIRED),
    (S.EVIDENCE_REQUIRED, E.SUBMIT_EVIDENCE, S.EVIDENCE_SUBMITTED),
    (S.EVIDENCE_SUBMITTED, E.START_VALIDATION, S.VALIDATING),
    (S.EVIDENCE_SUBMITTED, E.REASSIGN_REVIEWER, S.EVIDENCE_SUBMITTED),
    (S.EVIDENCE_SUBMITTED, E.WITHDRAW, S.IN_PROGRESS),
    (S.VALIDATING, E.VOTE_APPROVE, S.VALIDATING),
    (S.VALIDATING, E.VOTE_REJECT, S.VALIDATING),
    (S.VALIDATING, E.REASSIGN_REVIEWER, S.VALIDATING),
    (S.VALIDATING, E.WITHDRAW, S.IN_PROGRESS),
    (S.VALIDATING, E.APPROVE, S.VERIFIED),
    (S.VALIDATING, E.REJECT, S.REJECTED),
    (S.REJECTED, E.RESTART, S.IN_PROGRESS),
    # review mode (Revora)
    (S.VALIDATING, E.SUBMIT_REPORT, S.VALIDATING),
    (S.VALIDATING, E.COMPLETE_REVIEW, S.REVIEWED),
    (S.REVIEWED, E.NEW_VERSION, S.IN_PROGRESS),
]


@pytest.mark.parametrize("source,event,target", ALLOWED)
def test_allowed_transitions(source, event, target):
    machine = VerificationMachine(source)
    step = machine.transition(event)
    assert machine.current_state == target
    assert step == {"from_state": source.value, "event": event.value, "to_state": target.value}


def test_table_contains_exactly_the_documented_edges():
    assert set(all_transitions()) == set(ALLOWED)


def test_every_undefined_pair_is_rejected():
    defined = {(s, e) for s, e, _ in ALLOWED}
    for state, event in itertools.product(S, E):
        if (state, event) in defined:
            continue
        machine = VerificationMachine(state)
        with pytest.raises(InvalidTransitionError):
            machine.transition(event)
        assert machine.current_state == state  # a refused event leaves the state untouched


def test_cannot_jump_from_created_to_verified():
    machine = VerificationMachine()
    for event in (E.APPROVE, E.REJECT, E.SUBMIT_EVIDENCE, E.START_VALIDATION):
        with pytest.raises(ValueError):
            machine.transition(event)
    assert machine.current_state == S.CREATED


def test_verified_is_terminal_and_accepting():
    assert is_terminal(S.VERIFIED)
    assert ACCEPTING_STATES == {S.VERIFIED}
    with pytest.raises(ValueError):
        get_next_state(S.VERIFIED, E.RESTART)


def test_every_state_reachable_from_initial_state():
    seen, frontier = {INITIAL_STATE}, [INITIAL_STATE]
    while frontier:
        for target in TRANSITIONS.get(frontier.pop(), {}).values():
            if target not in seen:
                seen.add(target)
                frontier.append(target)
    assert seen == set(S)


def test_every_event_has_a_required_actor_and_is_used():
    assert set(REQUIRED_ACTOR) == set(E)
    assert {e for _, e, _ in all_transitions()} == set(E)


def test_events_available_depend_on_role():
    assert set(events_available_to(S.VALIDATING, is_owner=True, is_reviewer=False)) == {E.REASSIGN_REVIEWER, E.WITHDRAW}
    assert set(events_available_to(S.VALIDATING, is_owner=False, is_reviewer=True)) == {E.VOTE_APPROVE, E.VOTE_REJECT, E.SUBMIT_REPORT}
    # APPROVE / REJECT are fired by the quorum rule, never by a person
    for owner, reviewer in ((True, False), (False, True), (True, True)):
        assert not {E.APPROVE, E.REJECT} & set(events_available_to(S.VALIDATING, owner, reviewer))
    # an owner who is also a reviewer still cannot review their own work
    assert not {E.VOTE_APPROVE, E.VOTE_REJECT} & set(events_available_to(S.VALIDATING, True, True))
    assert events_available_to(S.CREATED, is_owner=True, is_reviewer=False) == [E.START]
    assert REQUIRED_ACTOR[E.VOTE_APPROVE] is Actor.REVIEWER
    assert REQUIRED_ACTOR[E.APPROVE] is Actor.SYSTEM
