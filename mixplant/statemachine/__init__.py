"""Stage machines, the interlock table, the pre gate and rollback windows."""

from __future__ import annotations

from .gate import GateResult, PreGate
from .guard import RollbackDenial, RollbackGuard
from .interlock import InterlockDecision, InterlockEngine, InterlockRule
from .latch import Latch, LatchEvent
from .machine import PhaseStep, SequenceMachine
from .phases import LINE_SEQUENCES, phase_names, sequence_for
from .rollback import (
    CONFIRMED,
    OPEN,
    ROLLBACK_BLOCKED,
    ROLLBACK_CONFIRMED,
    ROLLBACK_OPENED,
    RollbackEvent,
    RollbackWindow,
)
from .rollback_policy import (
    BLOCK_ALL,
    BLOCK_RELATED,
    STABLE_PHASES,
    RollbackPolicy,
    policy_for,
    previous_stable_phase,
    stable_phases,
)

__all__ = [
    "BLOCK_ALL",
    "BLOCK_RELATED",
    "CONFIRMED",
    "GateResult",
    "InterlockDecision",
    "InterlockEngine",
    "InterlockRule",
    "LINE_SEQUENCES",
    "Latch",
    "LatchEvent",
    "OPEN",
    "PhaseStep",
    "PreGate",
    "ROLLBACK_BLOCKED",
    "ROLLBACK_CONFIRMED",
    "ROLLBACK_OPENED",
    "RollbackDenial",
    "RollbackEvent",
    "RollbackGuard",
    "RollbackPolicy",
    "RollbackWindow",
    "SequenceMachine",
    "STABLE_PHASES",
    "phase_names",
    "policy_for",
    "previous_stable_phase",
    "sequence_for",
    "stable_phases",
]
