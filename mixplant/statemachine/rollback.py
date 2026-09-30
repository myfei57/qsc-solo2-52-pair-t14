"""The rollback window each line moves through.

A line is either running normally or sitting inside one rollback window. The
window lifecycle is a strict sequence::

    rollback.open -> rollback.blocked attempts (zero or more) -> rollback.confirm

Nothing may be confirmed before it was opened, a window may not be opened
twice, and every event is appended to an immutable history. The history is the
audit side's source of truth: a blocked action is recorded in arrival order at
the moment it is refused, so the ordering of denials can never be rearranged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ..errors import RollbackWindowError, ValidationError
from .rollback_policy import RollbackPolicy

OPEN = "open"
CONFIRMED = "confirmed"

ROLLBACK_OPENED = "rollback.opened"
ROLLBACK_BLOCKED = "rollback.action_blocked"
ROLLBACK_CONFIRMED = "rollback.confirmed"


@dataclass(frozen=True, slots=True)
class RollbackEvent:
    """One thing that happened to a rollback window, in order."""

    name: str
    tick: int
    detail: dict[str, object]

    def describe(self) -> dict[str, object]:
        return {"name": self.name, "tick": self.tick, "detail": dict(self.detail)}


class RollbackWindow:
    """Holds at most one open rollback for one line and its full history."""

    __slots__ = (
        "_line",
        "_policy",
        "_from_phase",
        "_opened_tick",
        "_reason",
        "_rollback_id",
        "_confirmed",
        "_events",
    )

    def __init__(self, line: str) -> None:
        self._line = line
        self._policy: RollbackPolicy | None = None
        self._from_phase = ""
        self._opened_tick = 0
        self._reason = ""
        self._rollback_id = ""
        self._confirmed: RollbackEvent | None = None
        self._events: list[RollbackEvent] = []

    @property
    def line(self) -> str:
        return self._line

    @property
    def open(self) -> bool:
        return self._policy is not None and self._confirmed is None

    @property
    def policy(self) -> RollbackPolicy | None:
        return self._policy

    def history(self) -> tuple[RollbackEvent, ...]:
        """Return every rollback event, oldest first. Append-only."""

        return tuple(self._events)

    def open_window(
        self,
        *,
        policy: RollbackPolicy,
        from_phase: str,
        reason: str,
        tick: int,
        rollback_id: str,
    ) -> RollbackEvent:
        """Open the window; a reason is mandatory and nesting is refused."""

        if not reason or not reason.strip():
            raise ValidationError("a rollback must state its reason", line=self._line)
        if self.open:
            raise RollbackWindowError(
                "a rollback is already open and must be realigned first",
                line=self._line,
                target_phase=self._policy.target_phase if self._policy else None,
                rollback_id=rollback_id,
            )
        if policy.line != self._line:
            raise ValidationError("rollback policy belongs to another line", line=self._line, policy=policy.line)
        self._policy = policy
        self._from_phase = from_phase
        self._opened_tick = tick
        self._reason = reason.strip()
        self._rollback_id = rollback_id
        event = RollbackEvent(
            ROLLBACK_OPENED,
            tick,
            {
                "rollback_id": rollback_id,
                "line": self._line,
                "from_phase": from_phase,
                "target_phase": policy.target_phase,
                "scope": policy.scope,
                "reason": self._reason,
            },
        )
        self._events.append(event)
        return event

    def note_blocked(self, *, action: str, tick: int, rollback_id: str) -> RollbackEvent:
        """Record an action refused by an open window, in arrival order."""

        policy = self._require_open(rollback_id)
        event = RollbackEvent(
            ROLLBACK_BLOCKED,
            tick,
            {
                "rollback_id": rollback_id,
                "line": self._line,
                "action": action,
                "scope": policy.scope,
                "target_phase": policy.target_phase,
            },
        )
        self._events.append(event)
        return event

    def confirm(
        self,
        *,
        reason: str,
        tick: int,
        confirmer: str,
        realigned_phase: str,
        rollback_id: str,
    ) -> RollbackEvent:
        """Close the window after a person confirms the line is realigned."""

        policy = self._require_open(rollback_id)
        if not reason or not reason.strip():
            raise ValidationError(
                "a realignment confirmation must state why it is safe to continue",
                line=self._line,
                rollback_id=rollback_id,
            )
        event = RollbackEvent(
            ROLLBACK_CONFIRMED,
            tick,
            {
                "rollback_id": rollback_id,
                "line": self._line,
                "from_phase": self._from_phase,
                "target_phase": policy.target_phase,
                "realigned_phase": realigned_phase,
                "reason": reason.strip(),
                "confirmer": confirmer,
                "opened_tick": self._opened_tick,
                "rollback_reason": self._reason,
            },
        )
        self._events.append(event)
        self._confirmed = event
        self._policy = None
        self._rollback_id = ""
        return event

    def describe(self) -> dict[str, object] | None:
        """Describe the open window for the console, or None when closed."""

        if not self.open or self._policy is None:
            return None
        return {
            "rollback_id": self._rollback_id,
            "line": self._line,
            "from_phase": self._from_phase,
            "target_phase": self._policy.target_phase,
            "scope": self._policy.scope,
            "reason": self._reason,
            "opened_tick": self._opened_tick,
            "blocked_actions": [
                str(event.detail["action"])
                for event in self._events
                if event.name == ROLLBACK_BLOCKED
            ],
            "policy": self._policy.describe(),
        }

    def _require_open(self, rollback_id: str) -> RollbackPolicy:
        if self._policy is None or self._confirmed is not None:
            raise RollbackWindowError(
                "no rollback is open on this line",
                line=self._line,
                rollback_id=rollback_id,
            )
        return self._policy

    def replay_block(self, *, action: str, tick: int, rollback_id: str) -> RollbackEvent:
        """Append a blocked action rebuilt from the record stream."""

        self._require_open(rollback_id or self._rollback_id)
        event = RollbackEvent(
            ROLLBACK_BLOCKED,
            tick,
            {
                "rollback_id": rollback_id or self._rollback_id,
                "line": self._line,
                "action": action,
                "scope": self._policy.scope if self._policy else "",
                "target_phase": self._policy.target_phase if self._policy else "",
                "replayed": True,
            },
        )
        self._events.append(event)
        return event

    def replay_confirm(self, *, reason: str, tick: int, confirmer: str, realigned_phase: str, rollback_id: str) -> None:
        """Close a window using a confirmation rebuilt from the record stream."""

        policy = self._require_open(rollback_id or self._rollback_id)
        self._events.append(
            RollbackEvent(
                ROLLBACK_CONFIRMED,
                tick,
                {
                    "rollback_id": rollback_id or self._rollback_id,
                    "line": self._line,
                    "from_phase": self._from_phase,
                    "target_phase": policy.target_phase,
                    "realigned_phase": realigned_phase,
                    "reason": reason,
                    "confirmer": confirmer,
                    "replayed": True,
                },
            )
        )
        self._policy = None
        self._rollback_id = ""
        self._confirmed = self._events[-1]

    def hydrate_open(
        self,
        *,
        policy: RollbackPolicy,
        from_phase: str,
        reason: str,
        tick: int,
        rollback_id: str = "",
    ) -> None:
        """Adopt an open window rebuilt from the record stream on restart."""

        self._policy = policy
        self._from_phase = from_phase
        self._reason = reason
        self._opened_tick = tick
        self._rollback_id = rollback_id
        self._confirmed = None
        self._events.append(
            RollbackEvent(
                ROLLBACK_OPENED,
                tick,
                {
                    "rollback_id": "",
                    "line": self._line,
                    "from_phase": from_phase,
                    "target_phase": policy.target_phase,
                    "scope": policy.scope,
                    "reason": reason,
                    "hydrated": True,
                },
            )
        )
