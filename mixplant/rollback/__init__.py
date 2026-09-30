"""Controlled stage rollback and realignment."""

from __future__ import annotations

from .policy import ROLLBACK_KIND, REALIGN_KIND, RollbackService
from ..statemachine.rollback import (
    ACTION_LINES,
    BlockedAction,
    RollbackEpisode,
    RollbackRegistry,
    action_line,
)
from ..statemachine.rollback_policy import (
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
    "ACTION_LINES",
    "BlockedAction",
    "FREEZE_SCOPES",
    "REALIGN_KIND",
    "ROLLBACK_KIND",
    "RollbackEpisode",
    "RollbackRegistry",
    "RollbackService",
    "SCOPE_FULL",
    "SCOPE_RELATED",
    "action_line",
    "describe_line",
    "is_stable",
    "previous_stable",
    "stable_stages",
    "withdraw_scope",
]
