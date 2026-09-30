"""Stage machines, the interlock table and the pre gate."""

from __future__ import annotations

from .gate import GateResult, PreGate
from .interlock import InterlockDecision, InterlockEngine, InterlockRule
from .latch import Latch, LatchEvent
from .machine import PhaseStep, SequenceMachine
from .phases import LINE_SEQUENCES, phase_names, sequence_for
from .rollback import (
    ACTION_LINES,
    BlockedAction,
    RollbackEpisode,
    RollbackRegistry,
    action_line,
)
from .rollback_policy import (
    FREEZE_SCOPES,
    SCOPE_FULL,
    SCOPE_RELATED,
    describe_line,
    is_stable,
    previous_stable,
    stable_stages,
    withdraw_scope,
)

__all__ = [
    "GateResult",
    "InterlockDecision",
    "InterlockEngine",
    "InterlockRule",
    "Latch",
    "LatchEvent",
    "PhaseStep",
    "PreGate",
    "SequenceMachine",
    "ACTION_LINES",
    "BlockedAction",
    "RollbackEpisode",
    "RollbackRegistry",
    "SCOPE_FULL",
    "SCOPE_RELATED",
    "FREEZE_SCOPES",
    "action_line",
    "describe_line",
    "is_stable",
    "previous_stable",
    "stable_stages",
    "withdraw_scope",
    "phase_names",
    "sequence_for",
    "LINE_SEQUENCES",
]
