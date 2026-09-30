"""Combines a stage check with the interlock table."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ..errors import OrderViolationError, RollbackBlockedError
from .guard import RollbackGuard
from .interlock import InterlockDecision, InterlockEngine
from .machine import SequenceMachine


@dataclass(frozen=True, slots=True)
class GateResult:
    """Everything the caller needs to explain an allow or a refusal."""

    action: str
    allowed: bool
    phase: str | None
    decision: InterlockDecision
    rollback: Mapping[str, object] | None = None

    def describe(self) -> dict[str, object]:
        return {
            "action": self.action,
            "allowed": self.allowed,
            "phase": self.phase,
            "interlock": self.decision.describe(),
            "rollback": dict(self.rollback) if self.rollback is not None else None,
        }


class PreGate:
    """The single place a command is checked before it reaches a service."""

    __slots__ = ("_engine", "_rollback")

    def __init__(self, engine: InterlockEngine, rollback: RollbackGuard | None = None) -> None:
        self._engine = engine
        self._rollback = rollback

    @property
    def engine(self) -> InterlockEngine:
        return self._engine

    @property
    def rollback(self) -> RollbackGuard | None:
        return self._rollback

    def bind_rollback(self, guard: RollbackGuard) -> None:
        """Attach the rollback guard once the runtime owns every line."""

        self._rollback = guard

    def evaluate(
        self,
        action: str,
        *,
        facts: Mapping[str, bool],
        machine: SequenceMachine | None = None,
        required_phase: str | None = None,
        tick: int | None = None,
    ) -> GateResult:
        """Check the rollback guard, then the stage and the interlock table."""

        if self._rollback is not None:
            window = self._rollback.blocking_window(action)
            if window is not None and window.policy is not None:
                description = window.describe() or {}
                if tick is not None:
                    self._rollback.record_block(action, tick=tick)
                    description = window.describe() or description
                decision = InterlockDecision(
                    action=action,
                    allowed=False,
                    blocking=(f"rollback:{window.policy.scope}",),
                    missing=(window.policy.target_phase,),
                    message="action is blocked while the line is rolled back and awaiting realignment",
                )
                return GateResult(
                    action=action,
                    allowed=False,
                    phase=None if machine is None else machine.phase,
                    decision=decision,
                    rollback={
                        "line": window.line,
                        "target_phase": window.policy.target_phase,
                        "scope": window.policy.scope,
                        "rollback_id": description.get("rollback_id", ""),
                    },
                )
        decision = self._engine.evaluate(action, facts)
        if machine is not None and required_phase is not None and decision.allowed:
            try:
                machine.require_at_least(required_phase, action=action)
            except OrderViolationError as exc:
                decision = InterlockDecision(
                    action=action,
                    allowed=False,
                    blocking=(f"stage:{required_phase}",),
                    missing=(str(exc.context.get("current", "")), required_phase),
                    message=exc.message,
                )
        return GateResult(
            action=action,
            allowed=decision.allowed,
            phase=None if machine is None else machine.phase,
            decision=decision,
        )

    def require(
        self,
        action: str,
        *,
        facts: Mapping[str, bool],
        machine: SequenceMachine | None = None,
        required_phase: str | None = None,
        tick: int | None = None,
    ) -> GateResult:
        """Raise unless the gate would allow ``action``."""

        result = self.evaluate(
            action,
            facts=facts,
            machine=machine,
            required_phase=required_phase,
            tick=tick,
        )
        if result.allowed:
            return result
        if result.decision.blocking and result.decision.blocking[0].startswith("rollback:"):
            raise RollbackBlockedError(
                result.decision.message,
                action=action,
                phase=result.phase,
                **(
                    {key: value for key, value in result.rollback.items() if key != "rollback_id"}
                    if result.rollback
                    else {}
                ),
            )
        if result.decision.blocking and result.decision.blocking[0].startswith("stage:"):
            raise OrderViolationError(
                result.decision.message,
                action=action,
                required_phase=required_phase,
                phase=result.phase,
            )
        self._engine.require(action, facts)
        return result
