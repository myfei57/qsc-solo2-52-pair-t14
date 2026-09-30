"""The single authority that opens and confirms stage rollbacks."""

from __future__ import annotations

from typing import Any, Mapping

from ..audit.record import audit_entry
from ..errors import RollbackWindowError, ValidationError
from ..ids import SequenceIds
from ..statemachine.machine import SequenceMachine
from ..statemachine.rollback import (
    ROLLBACK_BLOCKED,
    ROLLBACK_CONFIRMED,
    ROLLBACK_OPENED,
)
from ..statemachine.rollback_policy import previous_stable_phase
from ..statemachine.guard import RollbackGuard

ROLLBACK_KIND = "rollback.opened"
ROLLBACK_CONFIRM_KIND = "rollback.confirmed"
ROLLBACK_BLOCK_KIND = "rollback.blocked"

AUDIT_ROLLBACK_OPENED = "rollback.opened"
AUDIT_ROLLBACK_CONFIRMED = "rollback.confirmed"
AUDIT_ROLLBACK_BLOCKED = "rollback.action_blocked"


class RollbackCoordinator:
    """Orchestrates rollback windows across machines, guard, store and audit."""

    origin = "rollback"

    def __init__(
        self,
        machines: Mapping[str, SequenceMachine],
        guard: RollbackGuard,
        *,
        store: Any,
        audit: Any,
        ids: SequenceIds,
        clock: Any,
        bus: Any,
    ) -> None:
        self._machines = machines
        self._guard = guard
        self._store = store
        self._audit = audit
        self._ids = ids
        self._clock = clock
        self._bus = bus

    @property
    def guard(self) -> RollbackGuard:
        return self._guard

    def lines(self) -> tuple[str, ...]:
        return tuple(self._machines)

    def rollback(self, line: str, reason: str) -> dict[str, Any]:
        """Roll a line back to its fixed previous stable stage.

        The reason is mandatory, the target is computed (never supplied by the
        caller), the window opens before the machine moves, and both the
        immutable stage conclusion and the rollback record are appended.
        """

        machine = self._machine(line)
        clean_reason = _require_reason(reason, line=line)
        target = previous_stable_phase(line, machine.order(), machine.phase)
        if target is None:
            raise RollbackWindowError(
                "the first stage of a line has no earlier stable stage to return to",
                line=line,
                phase=machine.phase,
            )
        rollback_id = self._ids.next("rb")
        tick = self._clock.now()
        from_phase = machine.phase

        # Ordering is part of the contract. The window opens first: if a
        # rollback is already active (or the reason is empty) this raises
        # before the machine moves, so a refused rollback leaves the stage
        # machine untouched. Only then does the machine move back, and the
        # durable writes follow in a fixed order: phase conclusion, rollback
        # record, audit entry.
        self._guard.open_window(
            line,
            target_phase=target,
            from_phase=from_phase,
            reason=clean_reason,
            tick=tick,
            rollback_id=rollback_id,
        )
        step = machine.rewind_to(target, tick=tick, reason=f"rollback: {clean_reason}")
        self._publish_phase(line, step)
        record = self._store.publish(
            ROLLBACK_KIND,
            self.origin,
            0,
            {
                "active": True,
                "rollback_id": rollback_id,
                "line": line,
                "from_phase": from_phase,
                "target_phase": target,
                "scope": self._guard.window(line).policy.scope,  # type: ignore[union-attr]
                "reason": clean_reason,
            },
        )
        self._audit_note(
            AUDIT_ROLLBACK_OPENED,
            {
                "rollback_id": rollback_id,
                "line": line,
                "from_phase": from_phase,
                "target_phase": target,
                "reason": clean_reason,
                "seq": record.seq,
            },
        )
        return {
            "rollback_id": rollback_id,
            "line": line,
            "from_phase": from_phase,
            "target_phase": target,
            "phase": machine.phase,
            "window": self._guard.window(line).describe(),
            "seq": record.seq,
        }

    def record_blocked_action(self, action: str) -> dict[str, Any] | None:
        """Persist one denial in arrival order; None when nothing blocks it.

        Called from the single command entry point, so two refusals of the
        same action can never be appended out of order.
        """

        window = self._guard.blocking_window(action)
        if window is None or window.policy is None:
            return None
        description = window.describe() or {}
        rollback_id = str(description.get("rollback_id", ""))
        tick = self._clock.now()
        self._guard.record_block(action, tick=tick)
        record = self._store.publish(
            ROLLBACK_BLOCK_KIND,
            self.origin,
            0,
            {
                "active": True,
                "rollback_id": rollback_id,
                "line": window.line,
                "action": action,
                "target_phase": window.policy.target_phase,
                "scope": window.policy.scope,
            },
        )
        self._audit_note(
            AUDIT_ROLLBACK_BLOCKED,
            {
                "rollback_id": rollback_id,
                "line": window.line,
                "action": action,
                "target_phase": window.policy.target_phase,
                "seq": record.seq,
            },
        )
        return {
            "rollback_id": rollback_id,
            "line": window.line,
            "action": action,
            "target_phase": window.policy.target_phase,
            "scope": window.policy.scope,
            "seq": record.seq,
        }

    def confirm_realignment(
        self,
        line: str,
        reason: str,
        *,
        confirmer: str = "operator",
    ) -> dict[str, Any]:
        """Close the open window after someone confirms realignment.

        The current machine phase is recorded as the realigned phase. The
        confirmation is refused unless a window is actually open.
        """

        machine = self._machine(line)
        window = self._guard.window(line)
        description = window.describe()
        if description is None:
            raise RollbackWindowError("no rollback is open on this line", line=line)
        rollback_id = str(description["rollback_id"])
        clean_reason = _require_reason(reason, line=line, what="realignment confirmation")
        tick = self._clock.now()
        realigned_phase = machine.phase
        window.confirm(
            reason=clean_reason,
            tick=tick,
            confirmer=confirmer,
            realigned_phase=realigned_phase,
            rollback_id=rollback_id,
        )
        record = self._store.publish(
            ROLLBACK_CONFIRM_KIND,
            self.origin,
            0,
            {
                "active": True,
                "rollback_id": rollback_id,
                "line": line,
                "realigned_phase": realigned_phase,
                "reason": clean_reason,
                "confirmer": confirmer,
            },
        )
        self._audit_note(
            AUDIT_ROLLBACK_CONFIRMED,
            {
                "rollback_id": rollback_id,
                "line": line,
                "realigned_phase": realigned_phase,
                "reason": clean_reason,
                "confirmer": confirmer,
                "seq": record.seq,
            },
        )
        return {
            "rollback_id": rollback_id,
            "line": line,
            "realigned_phase": realigned_phase,
            "phase": machine.phase,
            "open": False,
            "seq": record.seq,
        }

    def status(self) -> dict[str, Any]:
        """Describe the guard and the per line histories for the console."""

        return {
            "open_windows": self._guard.describe(),
            "lines": {
                line: {
                    "phase": machine.phase,
                    "open": self._guard.window(line).open,
                    "window": self._guard.window(line).describe(),
                    "history": [event.describe() for event in self._guard.window(line).history()],
                    "phase_history": [step.describe() for step in machine.history()],
                }
                for line, machine in self._machines.items()
            },
        }

    def replay(self, records: tuple[Any, ...]) -> None:
        """Rebuild every rollback window from committed records after restart."""

        for record in records:
            payload = record.payload
            if record.kind == ROLLBACK_KIND:
                line = str(payload["line"])
                self._guard.hydrate_open(
                    line,
                    target_phase=str(payload["target_phase"]),
                    from_phase=str(payload.get("from_phase", payload["target_phase"])),
                    reason=str(payload.get("reason", "replayed rollback")),
                    tick=record.tick,
                    rollback_id=str(payload.get("rollback_id", "")),
                )
            elif record.kind == ROLLBACK_BLOCK_KIND:
                line = str(payload["line"])
                self._guard.window(line).replay_block(
                    action=str(payload["action"]),
                    tick=record.tick,
                    rollback_id=str(payload.get("rollback_id", "")),
                )
            elif record.kind == ROLLBACK_CONFIRM_KIND:
                line = str(payload["line"])
                self._guard.window(line).replay_confirm(
                    reason=str(payload.get("reason", "replayed confirmation")),
                    tick=record.tick,
                    confirmer=str(payload.get("confirmer", "operator")),
                    realigned_phase=str(payload["realigned_phase"]),
                    rollback_id=str(payload.get("rollback_id", "")),
                )

    def _machine(self, line: str) -> SequenceMachine:
        machine = self._machines.get(line)
        if machine is None:
            raise ValidationError("unknown line", line=line, known=list(self._machines))
        return machine

    def _publish_phase(self, line: str, step: Any) -> None:
        self._store.publish(
            f"{line}.phase",
            self.origin,
            0,
            {"active": True, "phase": step.phase, "step": step.step, "reason": step.reason},
        )

    def _audit_note(self, name: str, detail: dict[str, Any]) -> None:
        entry = audit_entry(name=name, origin=self.origin, tick=self._clock.now(), detail=detail)
        self._audit.ledger.record(entry)
        self._store.publish("audit.entry", self.origin, 0, entry.describe())


def _require_reason(reason: str, *, line: str, what: str = "rollback") -> str:
    if not reason or not str(reason).strip():
        raise ValidationError(f"a {what} must state its reason", line=line)
    return str(reason).strip()
