"""View-model for the live state diagram.

Nodes are positioned by a fixed cosmetic layout, but *edges, the current
state, visited states and traversed edges all come from the real
transition table and the workflow's stored history* - nothing here
simulates the engine.

Drawing rules: ordinary transitions are straight arrows; transitions that
leave and re-enter the same state (votes, reviewer reassignment) are grouped
into one loop above the state; WITHDRAW (back to IN_PROGRESS) is an arc over
the top of the diagram.
"""
import math

from automata.states import WorkflowEvent, WorkflowState
from automata.transitions import INITIAL_STATE, REQUIRED_ACTOR, all_transitions

VIEW_W, VIEW_H = 940, 390
NODE_W, NODE_H = 128, 44
ROW1, ROW2 = 150, 338

_LAYOUT = {
    WorkflowState.CREATED: (80, ROW1),
    WorkflowState.IN_PROGRESS: (270, ROW1),
    WorkflowState.EVIDENCE_REQUIRED: (460, ROW1),
    WorkflowState.EVIDENCE_SUBMITTED: (650, ROW1),
    WorkflowState.VALIDATING: (840, ROW1),
    WorkflowState.REJECTED: (650, ROW2),
    WorkflowState.VERIFIED: (840, ROW2),
    WorkflowState.REVIEWED: (460, ROW2),
}

EVENT_LABELS = {
    WorkflowEvent.START: "Start work",
    WorkflowEvent.REQUEST_EVIDENCE: "Ready to provide proof",
    WorkflowEvent.SUBMIT_EVIDENCE: "Submit evidence for review",
    WorkflowEvent.START_VALIDATION: "Begin review",
    WorkflowEvent.VOTE_APPROVE: "Decision: approve",
    WorkflowEvent.VOTE_REJECT: "Decision: reject",
    WorkflowEvent.APPROVE: "Verified (approval policy met)",
    WorkflowEvent.REJECT: "Rejected (approval no longer possible)",
    WorkflowEvent.REASSIGN_REVIEWER: "Replace a reviewer",
    WorkflowEvent.WITHDRAW: "Withdraw submission to change evidence",
    WorkflowEvent.RESTART: "Restart after rejection",
    WorkflowEvent.SUBMIT_REPORT: "Submit a review report",
    WorkflowEvent.COMPLETE_REVIEW: "Review complete (enough reports)",
    WorkflowEvent.NEW_VERSION: "Start a new version",
}

STATE_LABELS = {
    WorkflowState.CREATED: "Created",
    WorkflowState.IN_PROGRESS: "In progress",
    WorkflowState.EVIDENCE_REQUIRED: "Evidence required",
    WorkflowState.EVIDENCE_SUBMITTED: "Evidence submitted",
    WorkflowState.VALIDATING: "Under review",
    WorkflowState.VERIFIED: "Verified",
    WorkflowState.REJECTED: "Rejected",
    WorkflowState.REVIEWED: "Review complete",
}

HAPPY_PATH = [
    WorkflowState.CREATED,
    WorkflowState.IN_PROGRESS,
    WorkflowState.EVIDENCE_REQUIRED,
    WorkflowState.EVIDENCE_SUBMITTED,
    WorkflowState.VALIDATING,
    WorkflowState.VERIFIED,
]

_PRIORITY = {"available": 4, "traversed": 3, "waiting": 2, "idle": 1}


def progress_percent(state_value):
    """Progress along the happy path; a rejected task sits at the review step."""
    try:
        state = WorkflowState(state_value)
    except ValueError:
        return 0
    if state == WorkflowState.REVIEWED:
        return 100          # the review is complete; this says nothing about readiness
    if state == WorkflowState.REJECTED:
        return round(100 * HAPPY_PATH.index(WorkflowState.VALIDATING) / (len(HAPPY_PATH) - 1))
    return round(100 * HAPPY_PATH.index(state) / (len(HAPPY_PATH) - 1))


def _clip(cx, cy, dx, dy):
    """Where a ray from a node centre leaves the node's rectangle."""
    hw, hh = NODE_W / 2, NODE_H / 2
    scale = min(
        hw / abs(dx) if dx else math.inf,
        hh / abs(dy) if dy else math.inf,
    )
    return cx + dx * scale, cy + dy * scale


def _label_position(x1, y1, x2, y2, sx, sy, ex, ey):
    """Place the event label: above horizontal edges, beside vertical ones."""
    if y1 == y2:
        return {"lx": round((x1 + x2) / 2, 1), "ly": round(y1 - NODE_H / 2 - 10, 1),
                "anchor": "middle"}
    if x1 == x2:
        return {"lx": round(x1 + 10, 1), "ly": round((sy + ey) / 2 + 4, 1), "anchor": "start"}
    return {"lx": round((sx + ex) / 2, 1), "ly": round((sy + ey) / 2 - 4, 1), "anchor": "middle"}


def build_fsm_view(current_state, history, available_events):
    """Build everything the template needs to draw the automaton.

    current_state    - workflow's stored state (str)
    history          - rows with from_state / event / to_state (real transitions)
    available_events - events the *viewer* may fire right now (WorkflowEvent list)
    """
    current = WorkflowState(current_state)
    traversed = {(h["from_state"], h["event"], h["to_state"]) for h in history}
    # Votes may be redacted for a reviewer who has not decided yet; count them as taken.
    visited = {INITIAL_STATE.value, current.value}
    for h in history:
        visited.add(h["from_state"])
        visited.add(h["to_state"])
    available = set(available_events)

    nodes = []
    for state, (x, y) in _LAYOUT.items():
        nodes.append({
            "name": state.value,
            "label": STATE_LABELS[state],
            "x": x - NODE_W / 2,
            "y": y - NODE_H / 2,
            "cx": x,
            "cy": y,
            "current": state == current,
            "visited": state.value in visited,
            "kind": "good" if state == WorkflowState.VERIFIED
                    else "bad" if state == WorkflowState.REJECTED else "",
        })

    def status_of(from_state, event, to_state):
        key = (from_state.value, event.value, to_state.value)
        if key in traversed:
            return "traversed"
        if from_state == current and event in available:
            return "available"
        if from_state == current:
            return "waiting"        # defined here, but fired by someone else / by the policy
        return "idle"

    edges = []
    loops = {}                       # state -> grouped self-loop
    for from_state, event, to_state in all_transitions():
        status = status_of(from_state, event, to_state)
        actor = REQUIRED_ACTOR[event].value.lower()

        if from_state == to_state:
            group = loops.setdefault(from_state, {"events": [], "status": "idle", "actors": set()})
            group["events"].append(event.value)
            group["actors"].add(actor)
            if _PRIORITY[status] > _PRIORITY[group["status"]]:
                group["status"] = status
            continue

        x1, y1 = _LAYOUT[from_state]
        x2, y2 = _LAYOUT[to_state]

        if event == WorkflowEvent.WITHDRAW:
            # An arc over the top of the diagram, back to IN_PROGRESS.
            top1, top2 = y1 - NODE_H / 2, y2 - NODE_H / 2
            height = 70 + 34 * (from_state == WorkflowState.VALIDATING)
            sx, ex = x1 - 50, x2 + 20
            edges.append({
                "from": from_state.value, "event": event.value, "to": to_state.value,
                "path": (f"M {sx} {top1} C {sx} {top1 - height * 1.33} "
                         f"{ex} {top2 - height * 1.33} {ex} {top2}"),
                "lines": [event.value], "lx": round((sx + ex) / 2, 1),
                "ly": round(top1 - height - 8, 1), "anchor": "middle",
                "status": status, "actor": actor,
            })
            continue

        dx, dy = x2 - x1, y2 - y1
        sx, sy = _clip(x1, y1, dx, dy)
        ex, ey = _clip(x2, y2, -dx, -dy)
        edges.append({
            "from": from_state.value, "event": event.value, "to": to_state.value,
            "path": f"M {round(sx, 1)} {round(sy, 1)} L {round(ex, 1)} {round(ey, 1)}",
            "lines": [event.value],
            **_label_position(x1, y1, x2, y2, sx, sy, ex, ey),
            "status": status, "actor": actor,
        })

    for state, group in loops.items():
        x, y = _LAYOUT[state]
        top = y - NODE_H / 2
        cx = x + 22
        lines = group["events"]
        edges.append({
            "from": state.value, "event": " / ".join(lines), "to": state.value,
            "path": f"M {cx - 14} {top} C {cx - 26} {top - 36} {cx + 26} {top - 36} {cx + 14} {top}",
            "lines": lines, "lx": cx, "ly": round(top - 34 - 12 * (len(lines) - 1), 1),
            "anchor": "middle", "status": group["status"],
            "actor": "/".join(sorted(group["actors"])),
        })

    return {
        "width": VIEW_W,
        "height": VIEW_H,
        "node_w": NODE_W,
        "node_h": NODE_H,
        "nodes": nodes,
        "edges": edges,
    }
