"""The rollback service: stage rollback, the freeze and realignment.

The lifecycle is strictly ordered and entirely append only::

    rollback(line, reason)
        -> machine moves back to its previous *stable* stage (a new phase
           record; earlier phase conclusions stay in the stream)
        -> a ``rollback.started`` record holds the reason and the snapshotted
           freeze breadth
        -> governed actions covered by the breadth are refused by the gate and
           each refusal is audited, in tick order
    realign(line, confirmed_by)
        -> a ``rollback.realigned`` record names the person who confirmed the
           line is aligned, the freeze clears and forward work resumes

Nothing is tombstoned: a rollback adds records, it never removes or rewrites
the conclusions the line had already reached.
"""

from __future__ import annotations

from typing import Any

from ..audit.audit import (
    ROLLBACK_BLOCKED_KIND,
    ROLLBACK_REALIGNED_KIND,
    ROLLBACK_STARTED_KIND,
)
from ..audit.log import AuditLedger
from ..audit.record import audit_entry
from ..errors import OrderViolationError, ValidationError
from ..event.bus import Event
from ..event.topics import ROLLBACK
from ..service_base import PlantService
from ..statemachine.machine import PhaseStep, SequenceMachine
from ..statemachine.rollback import RollbackRegistry
from ..statemachine.rollback_policy import describe_line, previous_stable, withdraw_scope

ROLLBACK_KIND = "rollback.started"
REALIGN_KIND = "rollback.realigned"


class RollbackService(PlantService):
    """Owns the rollback registry and the rollback/realign command surface."""

    origin = "rollback"
    line = ""
    subject = "rollback"

    def __init__(
        self,
        context,
        registry: RollbackRegistry,
        machines: dict[str, SequenceMachine],
        ledger: AuditLedger,
    ) -> None:
        # The service spans every line; the batching machine only satisfies the
        # base class and is never advanced through the inherited helpers.
        super().__init__(context, machines["batch"])
        self.registry = registry
        self.machines = machines
        self.ledger = ledger

    # -- commands -----------------------------------------------------------

    def rollback(self, line: str, *, reason: str) -> dict[str, Any]:
        """Return a line to its previous stable stage and freeze it."""

        machine = self._machine(line)
        reason = (reason or "").strip()
        if not reason:
            raise ValidationError("a rollback must state why it is taken", line=line)

        # Any open freeze (a full one holds the whole plant) must be realigned
        # before another rollback can be opened, so rollback/realign ordering
        # can never interleave.
        open_any = self.registry.open_episode()
        if open_any is not None:
            raise OrderViolationError(
                "a rollback must be realigned before another one can be opened",
                line=line,
                open_line=open_any.line,
            )

        current = machine.phase
        target = previous_stable(line, current)
        if target is None:
            raise OrderViolationError(
                "the line already sits at its first stable stage",
                line=line,
                phase=current,
            )
        # The breadth follows the conclusion being withdrawn (current stage),
        # not the destination the line returns to.
        scope = withdraw_scope(line, current)

        # Move the stage machine first (appends to its in-memory history), then
        # persist in one fixed order: phase, then the rollback that explains it.
        step = machine.rewind_to(target, tick=self.context.clock.now(), reason=f"rollback: {reason}")
        phase_record = self._record_phase(line, step)
        rollback_id = f"RB-{self.store.last_seq() + 2:04d}"
        record = self.publish(
            ROLLBACK_KIND,
            {
                "active": True,
                "rollback_id": rollback_id,
                "line": line,
                "from_phase": current,
                "to_phase": target,
                "scope": scope,
                "reason": reason,
                "phase_seq": phase_record.seq,
            },
        )
        episode = self.registry.begin_episode(
            rollback_id=rollback_id,
            line=line,
            from_phase=current,
            to_phase=target,
            scope=scope,
            reason=reason,
            tick=record.tick,
            seq=record.seq,
        )
        self._audit(
            ROLLBACK_STARTED_KIND,
            "rollback.started",
            {
                "rollback_id": episode.rollback_id,
                "line": line,
                "from_phase": current,
                "to_phase": target,
                "scope": scope,
                "reason": reason,
            },
        )
        return {"rollback": episode.describe(), "seq": record.seq}

    def realign(self, line: str, *, confirmed_by: str, note: str | None = None) -> dict[str, Any]:
        """Confirm a frozen line is realigned and clear the freeze."""

        self._machine(line)
        confirmed_by = (confirmed_by or "").strip()
        if not confirmed_by:
            raise ValidationError("a realignment must name who confirmed it", line=line)
        note_value = note.strip() if isinstance(note, str) and note.strip() else None

        episode = self.registry.require_open(line)
        record = self.publish(
            REALIGN_KIND,
            {
                "active": False,
                "line": line,
                "rollback_id": episode.rollback_id,
                "from_phase": episode.from_phase,
                "to_phase": episode.to_phase,
                "confirmed_by": confirmed_by,
                "note": note_value,
            },
        )
        self.registry.close_episode(
            line=line,
            tick=record.tick,
            seq=record.seq,
            confirmed_by=confirmed_by,
            note=note_value,
        )
        self._audit(
            ROLLBACK_REALIGNED_KIND,
            "rollback.realigned",
            {
                "rollback_id": episode.rollback_id,
                "line": line,
                "to_phase": episode.to_phase,
                "confirmed_by": confirmed_by,
                "note": note_value,
            },
        )
        return {"rollback": episode.describe(), "seq": record.seq}

    def record_blocked(self, *, action: str, tick: int) -> dict[str, Any] | None:
        """Audit one governed action the freeze refused, in time order.

        The action is never executed; only the attempt is appended so the
        sequence of what was blocked during a freeze cannot be reordered.
        """

        item = self.registry.record_blocked(
            action=action,
            tick=tick,
            reason="attempted while a rollback freeze was open",
        )
        if item is None:
            return None
        self._audit(ROLLBACK_BLOCKED_KIND, "rollback.action_blocked", item.describe())
        return item.describe()

    # -- reads --------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "registry": self.registry.describe(),
            "policy": {line: describe_line(line) for line in self.machines},
        }

    def describe(self) -> dict[str, Any]:
        return self.status()

    # -- plumbing -----------------------------------------------------------

    def _machine(self, line: str) -> SequenceMachine:
        machine = self.machines.get(line)
        if machine is None:
            raise ValidationError("unknown line", line=line, known=sorted(self.machines))
        return machine

    def _record_phase(self, line: str, step: PhaseStep):
        return self.store.publish(
            f"{line}.phase",
            self.origin,
            self.generation(),
            {"active": True, "phase": step.phase, "step": step.step, "reason": step.reason},
        )

    def _audit(self, kind: str, name: str, detail: dict[str, Any]) -> None:
        """Write the audit ledger, an audit stream record and raise an event."""

        entry = audit_entry(name=name, origin=self.origin, tick=self.context.clock.now(), detail=detail)
        self.ledger.record(entry)
        self.store.publish(kind, self.origin, self.generation(), entry.describe())
        self.context.bus.publish(
            Event(
                topic=ROLLBACK,
                name=name,
                origin=self.origin,
                tick=self.context.clock.now(),
                payload=dict(detail),
            )
        )
