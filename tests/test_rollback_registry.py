"""The rollback registry resolves which open freeze covers an action."""

from __future__ import annotations

import pytest

from mixplant.errors import ValidationError
from mixplant.statemachine.rollback import RollbackRegistry, action_line
from mixplant.statemachine.rollback_policy import SCOPE_FULL, SCOPE_RELATED


def _open(registry, line="mix", scope=SCOPE_RELATED, from_phase="mixed", to_phase="idle", seq=10):
    return registry.begin_episode(
        rollback_id=f"RB-{seq:04d}",
        line=line,
        from_phase=from_phase,
        to_phase=to_phase,
        scope=scope,
        reason="why",
        tick=1,
        seq=seq,
    )


def test_action_prefix_resolves_to_its_line() -> None:
    assert action_line("agg.weigh") == "batch"
    assert action_line("moist.compensate") == "batch"
    assert action_line("mix.time") == "mix"
    assert action_line("chute.open") == "mix"
    assert action_line("pump.start") == "delivery"
    assert action_line("audit.note") is None
    assert action_line("clock.advance") is None


def test_related_freeze_only_covers_its_own_line() -> None:
    registry = RollbackRegistry()
    episode = _open(registry, line="mix", scope=SCOPE_RELATED)
    assert registry.episode_for_action("mix.feed") is episode
    assert registry.episode_for_action("chute.open") is episode
    # a governed action on another line keeps flowing
    assert registry.episode_for_action("agg.weigh") is None
    # ungoverned commands are never frozen so the freeze can be confirmed
    assert registry.episode_for_action("audit.note") is None


def test_full_freeze_covers_every_governed_action() -> None:
    registry = RollbackRegistry()
    episode = _open(registry, line="delivery", scope=SCOPE_FULL, from_phase="delivered")
    assert registry.episode_for_action("agg.weigh") is episode
    assert registry.episode_for_action("mix.start") is episode
    assert registry.episode_for_action("pump.start") is episode
    assert registry.episode_for_action("audit.note") is None


def test_a_line_can_only_hold_one_open_episode() -> None:
    registry = RollbackRegistry()
    _open(registry, line="mix")
    with pytest.raises(ValidationError):
        _open(registry, line="mix", seq=12)


def test_closing_requires_an_open_episode() -> None:
    registry = RollbackRegistry()
    with pytest.raises(ValidationError):
        registry.close_episode(line="mix", tick=2, seq=11, confirmed_by="lead", note=None)


def test_blocked_actions_are_kept_in_call_order() -> None:
    registry = RollbackRegistry()
    _open(registry, line="delivery", scope=SCOPE_FULL, from_phase="delivered", seq=5)
    registry.record_blocked(action="pump.start", tick=8, reason="x")
    registry.record_blocked(action="agg.weigh", tick=8, reason="x")
    registry.record_blocked(action="mix.start", tick=9, reason="x")
    episode = registry.episodes()[0]
    assert [item.action for item in episode.blocked] == ["pump.start", "agg.weigh", "mix.start"]
    assert [item.tick for item in episode.blocked] == [8, 8, 9]


def test_attach_blocked_reaches_a_since_closed_episode() -> None:
    registry = RollbackRegistry()
    _open(registry, line="mix", seq=3)
    registry.close_episode(line="mix", tick=5, seq=9, confirmed_by="lead", note=None)
    # the live lookup would miss it now; an explicit attach by id still works
    item = registry.attach_blocked(
        rollback_id="RB-0003", action="mix.feed", tick=4, scope=SCOPE_RELATED, line="mix", reason="r"
    )
    assert item.rollback_id == "RB-0003"
    assert registry.episodes()[0].blocked[0].action == "mix.feed"
    with pytest.raises(ValidationError):
        registry.attach_blocked(
            rollback_id="RB-9999", action="mix.feed", tick=4, scope=SCOPE_RELATED, line="mix", reason="r"
        )
