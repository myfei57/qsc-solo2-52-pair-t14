"""Combines a stage check with the interlock table."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ..errors import OrderViolationError, RollbackFrozenError
from .interlock import InterlockDecision, InterlockEngine
from .machine import SequenceMachine
from .rollback import RollbackRegistry, action_line

ROLLBACK_BLOCK_PREFIX = "rollback:"


@dataclass(frozen=True, slots=True)
class GateResult:
    """Everything the caller needs to explain an allow or a refusal."""

    action: str
    allowed: bool
    phase: str | None
    decision: InterlockDecision

    def describe(self) -> dict[str, object]:
        return {
            "action": self.action,
            "allowed": self.allowed,
            "phase": self.phase,
            "interlock": self.decision.describe(),
        }


class PreGate:
    """The single place a command is checked before it reaches a service."""

    __slots__ = ("_engine", "_rollbacks")

    def __init__(
        self,
        engine: InterlockEngine,
        rollbacks: RollbackRegistry | None = None,
    ) -> None:
        self._engine = engine
        self._rollbacks = rollbacks

    @property
    def engine(self) -> InterlockEngine:
        return self._engine

    @property
    def rollbacks(self) -> RollbackRegistry | None:
        return self._rollbacks

    def bind_rollbacks(self, rollbacks: RollbackRegistry) -> None:
        """Attach the rollback registry the gate consults for freezes."""

        self._rollbacks = rollbacks

    def freeze_for(self, action: str):
        """Return the open rollback episode that freezes ``action``."""

        if self._rollbacks is None:
            return None
        return self._rollbacks.episode_for_action(action)

    def evaluate(
        self,
        action: str,
        *,
        facts: Mapping[str, bool],
        machine: SequenceMachine | None = None,
        required_phase: str | None = None,
    ) -> GateResult:
        """Check the freeze, then the stage requirement and the interlock table."""

        episode = self.freeze_for(action)
        if episode is not None:
            decision = InterlockDecision(
                action=action,
                allowed=False,
                blocking=(f"{ROLLBACK_BLOCK_PREFIX}{episode.scope}",),
                missing=(episode.line,),
                message=(
                    f"the {episode.line} line is rolled back to {episode.to_phase} "
                    f"and frozen until it is realigned"
                ),
            )
            return GateResult(
                action=action,
                allowed=False,
                phase=None if machine is None else machine.phase,
                decision=decision,
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
    ) -> GateResult:
        """Raise unless the gate would allow ``action``."""

        result = self.evaluate(action, facts=facts, machine=machine, required_phase=required_phase)
        if result.allowed:
            return result
        blocking = result.decision.blocking
        if blocking and blocking[0].startswith(ROLLBACK_BLOCK_PREFIX):
            episode = self.freeze_for(action)
            raise RollbackFrozenError(
                result.decision.message,
                action=action,
                line=None if episode is None else episode.line,
                scope=None if episode is None else episode.scope,
                to_phase=None if episode is None else episode.to_phase,
                action_line=action_line(action),
            )
        if blocking and blocking[0].startswith("stage:"):
            raise OrderViolationError(
                result.decision.message,
                action=action,
                required_phase=required_phase,
                phase=result.phase,
            )
        self._engine.require(action, facts)
        return result
