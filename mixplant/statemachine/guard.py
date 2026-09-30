"""The guard that denies actions while a rollback window is open.

This is the single authority the pre gate consults *before* the interlock
table. It owns one window per line and answers two questions:

* ``blocks(action)`` — does any open window currently deny this action?
* ``record_block(action, ...)`` — append the denial to that window's history,
  in arrival order, so the audit trail keeps the exact sequence of refusals.

Block scope (whole line vs. related actions only) comes from the declared
rollback policy, so the gate never invents its own rules.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import RollbackBlockedError, RollbackWindowError
from .rollback import RollbackWindow
from .rollback_policy import ACTION_LINES, RollbackPolicy, policy_for


@dataclass(frozen=True, slots=True)
class RollbackDenial:
    """Why an action was refused during a rollback window."""

    action: str
    line: str
    target_phase: str
    scope: str
    rollback_id: str

    def describe(self) -> dict[str, object]:
        return {
            "action": self.action,
            "line": self.line,
            "target_phase": self.target_phase,
            "scope": self.scope,
            "rollback_id": self.rollback_id,
        }


class RollbackGuard:
    """Holds the open rollback windows and evaluates actions against them."""

    __slots__ = ("_windows",)

    def __init__(self, lines: tuple[str, ...] | list[str]) -> None:
        self._windows: dict[str, RollbackWindow] = {line: RollbackWindow(line) for line in lines}

    def window(self, line: str) -> RollbackWindow:
        window = self._windows.get(line)
        if window is None:
            raise RollbackWindowError("unknown line", line=line)
        return window

    def lines(self) -> tuple[str, ...]:
        return tuple(self._windows)

    def open_window(
        self,
        line: str,
        *,
        target_phase: str,
        from_phase: str,
        reason: str,
        tick: int,
        rollback_id: str,
    ) -> RollbackPolicy:
        """Open the window for ``line`` and return the policy now in force."""

        policy = policy_for(line, target_phase)
        self.window(line).open_window(
            policy=policy,
            from_phase=from_phase,
            reason=reason,
            tick=tick,
            rollback_id=rollback_id,
        )
        return policy

    def blocking_window(self, action: str) -> RollbackWindow | None:
        """Return the open window that denies ``action``, if any."""

        line = ACTION_LINES.get(action)
        if line is None:
            return None
        window = self._windows[line]
        if window.open and window.policy is not None and window.policy.blocks(action):
            return window
        return None

    def check(self, action: str) -> None:
        """Raise when an open window denies ``action``."""

        window = self.blocking_window(action)
        if window is None or window.policy is None:
            return
        raise RollbackBlockedError(
            "action is blocked while the line is rolled back and awaiting realignment",
            action=action,
            line=window.line,
            target_phase=window.policy.target_phase,
            scope=window.policy.scope,
        )

    def record_block(self, action: str, *, tick: int) -> RollbackDenial | None:
        """Append a denial to the blocking window's ordered history."""

        window = self.blocking_window(action)
        if window is None or window.policy is None:
            return None
        rollback_id = str((window.describe() or {}).get("rollback_id", ""))
        window.note_blocked(action=action, tick=tick, rollback_id=rollback_id)
        return RollbackDenial(
            action=action,
            line=window.line,
            target_phase=window.policy.target_phase,
            scope=window.policy.scope,
            rollback_id=rollback_id,
        )

    def describe(self) -> dict[str, object]:
        """Describe every open window for the console."""

        open_windows = {line: window.describe() for line, window in self._windows.items() if window.open}
        return {
            "open": [window for window in open_windows.values() if window is not None],
            "lines": open_windows,
        }

    def hydrate_open(
        self,
        line: str,
        *,
        target_phase: str,
        from_phase: str,
        reason: str,
        tick: int,
        rollback_id: str = "",
    ) -> None:
        """Rebuild an open window from the record stream after a restart."""

        policy = policy_for(line, target_phase)
        self.window(line).hydrate_open(
            policy=policy,
            from_phase=from_phase,
            reason=reason,
            tick=tick,
            rollback_id=rollback_id,
        )
