"""Which stages are stable rollback targets and how hard a rollback freezes.

A rollback never goes back "one step": it returns a line to the most recent
*stable* stage, so the operator always lands on a known-good point instead of
halfway through a stage.  That target is fixed by :func:`previous_stable`.

The breadth of the freeze that follows is a different question.  It depends on
the conclusion being withdrawn — the stage the line is rolled back *from* — not
on the destination, because the destination is always earlier and would make
the strongest freeze unreachable:

* withdrawing an in-line conclusion (``related``) blocks only the actions that
  belong to the rolled back line;
* withdrawing a consequence that crosses a line boundary — material dropped,
  concrete discharged or delivered (``full``) — blocks every governed action on
  every line until somebody confirms realignment.

The breadth is snapshotted onto the rollback record at open time, so a later
policy edit can neither widen nor narrow an open freeze.
"""

from __future__ import annotations

from ..errors import ValidationError
from .phases import LINE_SEQUENCES

SCOPE_RELATED = "related"
SCOPE_FULL = "full"
FREEZE_SCOPES = (SCOPE_RELATED, SCOPE_FULL)

# Stable stages a line may be returned to, in declared order.
STABLE_STAGES: dict[str, frozenset[str]] = {
    "batch": frozenset({"idle", "weighed", "dropped"}),
    "mix": frozenset({"idle", "mixed", "discharged"}),
    "delivery": frozenset({"idle", "delivered"}),
}

# Freeze breadth decided by the conclusion being withdrawn (the "from" stage).
# Stages not named default to ``related``; only the cross-line physical
# consequences freeze the whole plant.
WITHDRAW_FREEZE: dict[str, dict[str, str]] = {
    "batch": {
        "dropped": SCOPE_FULL,
    },
    "mix": {
        "discharged": SCOPE_FULL,
    },
    "delivery": {
        "delivered": SCOPE_FULL,
    },
}

_DEFAULT_SCOPE = SCOPE_RELATED


def stable_stages(line: str) -> tuple[str, ...]:
    """Return the stable stages of ``line`` in declared order."""

    table = STABLE_STAGES.get(line)
    if table is None:
        raise ValidationError("unknown line", line=line)
    order = tuple(str(phase.value) for phase in LINE_SEQUENCES[line])
    return tuple(stage for stage in order if stage in table)


def is_stable(line: str, phase: str) -> bool:
    """Return whether ``phase`` is a stage a rollback may start from."""

    table = STABLE_STAGES.get(line)
    if table is None:
        raise ValidationError("unknown line", line=line)
    return phase in table


def withdraw_scope(line: str, phase: str) -> str:
    """Return the freeze breadth for withdrawing ``phase`` on ``line``."""

    if phase not in tuple(str(p.value) for p in LINE_SEQUENCES[line]):
        raise ValidationError("stage is not part of this line", line=line, phase=phase)
    return WITHDRAW_FREEZE.get(line, {}).get(phase, _DEFAULT_SCOPE)


def previous_stable(line: str, phase: str) -> str | None:
    """Return the stable stage immediately before ``phase``.

    Returns ``None`` when ``phase`` already sits at the first stable stage and
    therefore has nothing to return to.  A rollback may only be started from a
    stable stage, so an intermediate phase is rejected.
    """

    stages = stable_stages(line)
    previous: str | None = None
    for stage in stages:
        if stage == phase:
            return previous
        previous = stage
    raise ValidationError("a rollback can only start from a stable stage", line=line, phase=phase)


def describe_line(line: str) -> dict[str, object]:
    """Describe the rollback policy of one line for the console."""

    declared = tuple(str(phase.value) for phase in LINE_SEQUENCES[line])
    return {
        "stable": list(stable_stages(line)),
        "withdraw_freeze": {stage: withdraw_scope(line, stage) for stage in declared},
    }
