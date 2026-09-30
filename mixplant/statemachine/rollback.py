"""The rollback lifecycle: one open freeze per line, closed by a realignment.

A :class:`RollbackEpisode` is an append only fact: once a rollback is recorded
its reason and freeze breadth never change, and it is only closed by a matching
realignment.  The registry derives "is anything frozen, and does it cover this
action" in one place so the gate, the services and a restarted process all give
the same answer.

Governed actions are the interlock-table actions (``"agg.weigh"``,
``"mix.time"`` ...).  Their leading segment is the line they belong to:
``agg`` and ``moist`` act on the batching line, ``cement``/``water``/``mix``/
``chute`` on the mixing line and ``pump``/``sampler`` on delivery.  Audit,
clock, snapshot and rollback commands are not governed actions and are never
frozen, so a freeze can always be inspected and confirmed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..errors import ValidationError
from .rollback_policy import SCOPE_FULL

#: governed action prefix -> the line it physically belongs to
ACTION_LINES: dict[str, str] = {
    "recipe": "batch",
    "agg": "batch",
    "moist": "batch",
    "cement": "mix",
    "water": "mix",
    "mix": "mix",
    "chute": "mix",
    "pump": "delivery",
    "sampler": "delivery",
}


def action_line(action: str) -> str | None:
    """Return the line a governed action belongs to, or ``None`` if ungoverned."""

    if not action or "." not in action:
        return None
    return ACTION_LINES.get(action.split(".", 1)[0])


@dataclass(frozen=True, slots=True)
class BlockedAction:
    """One governed action refused while a freeze was open, in time order."""

    action: str
    line: str
    tick: int
    rollback_id: str
    scope: str
    reason: str

    def describe(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "line": self.line,
            "tick": self.tick,
            "rollback_id": self.rollback_id,
            "scope": self.scope,
            "reason": self.reason,
        }


@dataclass(slots=True)
class RollbackEpisode:
    """A rollback that is holding a line frozen until it is realigned."""

    rollback_id: str
    line: str
    from_phase: str
    to_phase: str
    scope: str
    reason: str
    opened_tick: int
    opened_seq: int
    blocked: list[BlockedAction] = field(default_factory=list)
    realigned_tick: int | None = None
    realigned_seq: int | None = None
    confirmed_by: str | None = None
    realign_note: str | None = None

    @property
    def open(self) -> bool:
        return self.realigned_tick is None

    def describe(self) -> dict[str, Any]:
        return {
            "rollback_id": self.rollback_id,
            "line": self.line,
            "from_phase": self.from_phase,
            "to_phase": self.to_phase,
            "scope": self.scope,
            "reason": self.reason,
            "opened_tick": self.opened_tick,
            "opened_seq": self.opened_seq,
            "blocked": [item.describe() for item in self.blocked],
            "realigned_tick": self.realigned_tick,
            "realigned_seq": self.realigned_seq,
            "confirmed_by": self.confirmed_by,
            "realign_note": self.realign_note,
        }


class RollbackRegistry:
    """Holds every rollback episode, open and closed, in opening order."""

    __slots__ = ("_episodes",)

    def __init__(self) -> None:
        self._episodes: list[RollbackEpisode] = []

    def begin_episode(
        self,
        *,
        line: str,
        from_phase: str,
        to_phase: str,
        scope: str,
        reason: str,
        tick: int,
        seq: int,
        rollback_id: str | None = None,
    ) -> RollbackEpisode:
        """Record the opening of a freeze after its stream record is committed."""

        if self.is_open(line):
            raise ValidationError("the line is already held by an open rollback", line=line)
        episode = RollbackEpisode(
            rollback_id=rollback_id,
            line=line,
            from_phase=from_phase,
            to_phase=to_phase,
            scope=scope,
            reason=reason,
            opened_tick=tick,
            opened_seq=seq,
        )
        self._episodes.append(episode)
        return episode

    def close_episode(
        self,
        *,
        line: str,
        tick: int,
        seq: int,
        confirmed_by: str,
        note: str | None,
    ) -> RollbackEpisode:
        """Close the open freeze on ``line`` once its realignment is recorded."""

        episode = self.require_open(line)
        episode.realigned_tick = tick
        episode.realigned_seq = seq
        episode.confirmed_by = confirmed_by
        episode.realign_note = note
        return episode

    def is_open(self, line: str | None = None) -> bool:
        """Return whether any freeze is open (optionally restricted to ``line``)."""

        return self.open_episode(line) is not None

    def open_episode(self, line: str | None = None) -> RollbackEpisode | None:
        """Return the open episode for ``line``, or any open full freeze.

        When ``line`` is given, an open full-scope freeze on another line still
        matches because a full freeze blocks the whole plant.
        """

        full: RollbackEpisode | None = None
        for episode in reversed(self._episodes):
            if not episode.open:
                continue
            if line is None:
                return episode
            if episode.line == line:
                return episode
            if episode.scope == SCOPE_FULL:
                full = episode
        return full

    def require_open(self, line: str) -> RollbackEpisode:
        episode = self.open_episode(line)
        if episode is None or episode.line != line:
            raise ValidationError("the line has no open rollback to realign", line=line)
        return episode

    def episode_for_action(self, action: str) -> RollbackEpisode | None:
        """Return the open episode that freezes ``action``, if any.

        A full freeze wins regardless of line; otherwise the freeze on the
        action's own line applies.  Ungoverned actions are never frozen.
        """

        line = action_line(action)
        if line is None:
            return None
        return self.open_episode(line)

    def record_blocked(self, *, action: str, tick: int, reason: str) -> BlockedAction | None:
        """Remember a refused action under the episode that blocked it."""

        episode = self.episode_for_action(action)
        if episode is None:
            return None
        item = BlockedAction(
            action=action,
            line=action_line(action) or "",
            tick=tick,
            rollback_id=episode.rollback_id,
            scope=episode.scope,
            reason=reason,
        )
        episode.blocked.append(item)
        return item

    def attach_blocked(
        self,
        *,
        rollback_id: str,
        action: str,
        tick: int,
        scope: str,
        line: str,
        reason: str,
    ) -> BlockedAction:
        """Attach a replayed blocked attempt to a specific episode.

        Used during restart, where the episode may already have been realigned
        by a later record and therefore would no longer be returned by the
        live "is it frozen now" lookup.
        """

        for episode in self._episodes:
            if episode.rollback_id == rollback_id:
                item = BlockedAction(
                    action=action,
                    line=line,
                    tick=tick,
                    rollback_id=rollback_id,
                    scope=scope,
                    reason=reason,
                )
                episode.blocked.append(item)
                return item
        raise ValidationError("blocked action has no rollback episode", rollback_id=rollback_id, action=action)

    def episodes(self) -> tuple[RollbackEpisode, ...]:
        return tuple(self._episodes)

    def hydrate_open(
        self,
        *,
        rollback_id: str,
        line: str,
        from_phase: str,
        to_phase: str,
        scope: str,
        reason: str,
        opened_tick: int,
        opened_seq: int,
    ) -> RollbackEpisode:
        """Adopt an open episode rebuilt from the stream during a restart."""

        episode = RollbackEpisode(
            rollback_id=rollback_id,
            line=line,
            from_phase=from_phase,
            to_phase=to_phase,
            scope=scope,
            reason=reason,
            opened_tick=opened_tick,
            opened_seq=opened_seq,
        )
        self._episodes.append(episode)
        return episode

    def hydrate_close(self, *, line: str, tick: int, seq: int, confirmed_by: str, note: str | None) -> None:
        """Mark an episode closed while rebuilding from the stream."""

        for episode in reversed(self._episodes):
            if episode.line == line and episode.open:
                episode.realigned_tick = tick
                episode.realigned_seq = seq
                episode.confirmed_by = confirmed_by
                episode.realign_note = note
                return
        raise ValidationError("realignment has no open rollback to close", line=line)

    def describe(self) -> dict[str, Any]:
        open_episodes = [episode.describe() for episode in self._episodes if episode.open]
        return {
            "frozen": bool(open_episodes),
            "open": open_episodes,
            "episodes": [episode.describe() for episode in self._episodes],
        }
