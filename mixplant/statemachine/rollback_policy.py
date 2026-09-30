"""Declared rollback policy for each line.

The rollback rule is fixed on purpose: a rollback never rewinds a single step
and never jumps to an arbitrary stage. It always returns the line to the most
recent *stable* stage before the one it currently occupies. Intermediate
stages (weighing, water in, cement in, pumping) are transient and can never be
a rollback target.

While a rollback window is open the policy also decides, per target stage,
whether every action of the line is blocked or only the actions related to the
stage being redone. The mapping is data, never recomputed by callers, so the
gate, the stage machine and the audit trail all quote the same decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ..errors import ValidationError

BLOCK_ALL = "all"
BLOCK_RELATED = "related"


#: Stages a rollback is allowed to land on, in declared order.
STABLE_PHASES: Mapping[str, tuple[str, ...]] = {
    "batch": ("idle", "recipe_selected", "weighed", "dropped"),
    "mix": ("idle", "mixing", "mixed", "discharged"),
    "delivery": ("idle", "primed", "delivered"),
}

#: Per target stage: block the whole line or only the actions of that stage.
STAGE_SCOPES: Mapping[str, Mapping[str, str]] = {
    "batch": {
        "idle": BLOCK_ALL,
        "recipe_selected": BLOCK_RELATED,
        "weighed": BLOCK_RELATED,
        "dropped": BLOCK_RELATED,
    },
    "mix": {
        "idle": BLOCK_ALL,
        "mixing": BLOCK_RELATED,
        "mixed": BLOCK_RELATED,
        "discharged": BLOCK_RELATED,
    },
    "delivery": {
        "idle": BLOCK_ALL,
        "primed": BLOCK_RELATED,
        "delivered": BLOCK_RELATED,
    },
}

#: Actions considered "related" to redoing each stable stage.
RELATED_ACTIONS: Mapping[str, Mapping[str, frozenset[str]]] = {
    "batch": {
        "recipe_selected": frozenset(
            {
                "batch.declare",
                "agg.map_bin",
                "agg.weigh",
                "agg.persist",
                "agg.check",
                "agg.release",
                "agg.drop",
            }
        ),
        "weighed": frozenset({"agg.weigh", "agg.persist", "agg.check", "agg.release", "agg.drop"}),
        "dropped": frozenset({"agg.release", "agg.drop"}),
    },
    "mix": {
        "mixing": frozenset({"mix.start", "mix.feed", "mix.time", "mix.finish", "mix.stop"}),
        "mixed": frozenset({"mix.time", "mix.finish", "mix.schedule", "mix.stop"}),
        "discharged": frozenset({"chute.arrive", "chute.open", "chute.load", "chute.confirm", "chute.close"}),
    },
    "delivery": {
        "primed": frozenset({"water.prime", "pump.prime", "pump.start"}),
        "delivered": frozenset({"pump.start", "pump.stroke", "pump.pressure", "pump.finish", "sampler.draw"}),
    },
}

#: Which line each plant action belongs to. Rollback control commands and
#: plant-wide commands have no entry and are never blocked by a line window.
ACTION_LINES: Mapping[str, str] = {
    "recipe.publish": "batch",
    "recipe.select": "batch",
    "batch.declare": "batch",
    "agg.map_bin": "batch",
    "agg.weigh": "batch",
    "agg.persist": "batch",
    "agg.check": "batch",
    "agg.release": "batch",
    "agg.drop": "batch",
    "agg.calibrate": "batch",
    "moist.compensate": "batch",
    "cement.load_silo": "mix",
    "cement.open_gate": "mix",
    "cement.close_gate": "mix",
    "water.open_valve": "mix",
    "water.dispense": "mix",
    "mix.start": "mix",
    "mix.feed": "mix",
    "mix.time": "mix",
    "mix.finish": "mix",
    "mix.schedule": "mix",
    "mix.stop": "mix",
    "chute.arrive": "mix",
    "chute.open": "mix",
    "chute.load": "mix",
    "chute.confirm": "mix",
    "chute.close": "mix",
    "water.prime": "delivery",
    "pump.prime": "delivery",
    "pump.start": "delivery",
    "pump.stroke": "delivery",
    "pump.pressure": "delivery",
    "pump.finish": "delivery",
    "sampler.draw": "delivery",
    "sampler.release": "delivery",
    "sampler.file": "delivery",
}


@dataclass(frozen=True, slots=True)
class RollbackPolicy:
    """Everything a rollback window quotes once it is open."""

    line: str
    target_phase: str
    scope: str
    blocked_actions: frozenset[str]

    def describe(self) -> dict[str, object]:
        return {
            "line": self.line,
            "target_phase": self.target_phase,
            "scope": self.scope,
            "blocked_actions": sorted(self.blocked_actions),
        }

    def blocks(self, action: str) -> bool:
        """Return whether ``action`` is denied while this window is open."""

        if ACTION_LINES.get(action) != self.line:
            return False
        if self.scope == BLOCK_ALL:
            return True
        return action in self.blocked_actions


def stable_phases(line: str) -> tuple[str, ...]:
    """Return the stages a rollback may land on for ``line``."""

    phases = STABLE_PHASES.get(line)
    if phases is None:
        raise ValidationError("unknown line", line=line)
    return phases


def scope_for(line: str, target_phase: str) -> str:
    """Return the declared block scope for a rollback target."""

    scopes = STAGE_SCOPES.get(line)
    if scopes is None or target_phase not in scopes:
        raise ValidationError(
            "stage is not a stable rollback target",
            line=line,
            phase=target_phase,
            stable=list(stable_phases(line)),
        )
    return scopes[target_phase]


def related_actions(line: str, target_phase: str) -> frozenset[str]:
    """Return the actions related to redoing ``target_phase``."""

    return RELATED_ACTIONS.get(line, {}).get(target_phase, frozenset())


def policy_for(line: str, target_phase: str) -> RollbackPolicy:
    """Build the immutable policy quoted for the whole rollback window."""

    scope = scope_for(line, target_phase)
    blocked = related_actions(line, target_phase) if scope == BLOCK_RELATED else line_actions(line)
    return RollbackPolicy(line=line, target_phase=target_phase, scope=scope, blocked_actions=blocked)


def previous_stable_phase(line: str, order: tuple[str, ...], current_phase: str) -> str | None:
    """Return the fixed rollback target for a line at ``current_phase``.

    The target is the newest stable stage strictly before the current one. A
    transient stage (e.g. ``weighing``) rolls back to the stable stage before
    it; the first stage of a line has nowhere to go.
    """

    if line not in STABLE_PHASES:
        raise ValidationError("unknown line", line=line)
    try:
        current_index = order.index(current_phase)
    except ValueError as exc:
        raise ValidationError("stage is not part of this line", line=line, phase=current_phase) from exc
    target: str | None = None
    for phase in order[:current_index]:
        if phase in STABLE_PHASES[line]:
            target = phase
    return target


def line_actions(line: str) -> frozenset[str]:
    """Return every action that belongs to ``line``."""

    return frozenset(action for action, owner in ACTION_LINES.items() if owner == line)
