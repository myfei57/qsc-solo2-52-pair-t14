"""End to end rollback, freeze, realignment and restart behaviour."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from mixplant.config import StationConfig
from mixplant.errors import (
    OrderViolationError,
    RollbackFrozenError,
    ValidationError,
)
from mixplant.runtime import PlantRuntime
from tests.support import advance_to_mixed, run, run_full_cycle


def test_rollback_requires_a_reason(runtime: PlantRuntime) -> None:
    advance_to_mixed(runtime)
    with pytest.raises(ValidationError):
        run(runtime, "rollback.begin", {"line": "mix", "reason": "   "})


def test_rollback_returns_to_the_previous_stable_stage_and_freezes(runtime: PlantRuntime) -> None:
    advance_to_mixed(runtime)
    result = run(runtime, "rollback.begin", {"line": "mix", "reason": "water reading wrong"})
    episode = result["rollback"]
    assert episode["to_phase"] == "idle"
    assert episode["from_phase"] == "mixed"
    assert episode["scope"] == "related"
    assert runtime.machines["mix"].phase == "idle"
    assert runtime.rollbacks.is_open("mix")
    assert runtime.state()["rollback"]["frozen"] is True


def test_frozen_action_is_blocked_audited_and_never_executed(runtime: PlantRuntime) -> None:
    advance_to_mixed(runtime)
    run(runtime, "rollback.begin", {"line": "mix", "reason": "water reading wrong"})
    with pytest.raises(RollbackFrozenError):
        run(runtime, "mix.feed", {"component": "water"})
    episode = runtime.rollbacks.episodes()[-1].describe()
    assert [item["action"] for item in episode["blocked"]] == ["mix.feed"]
    kinds = {record.kind for record in runtime.store.committed_records()}
    assert "audit.rollback_blocked" in kinds


def test_related_freeze_keeps_other_lines_governed(runtime: PlantRuntime) -> None:
    advance_to_mixed(runtime)
    run(runtime, "rollback.begin", {"line": "mix", "reason": "rework mix"})
    # an action on another line is not rollback-blocked; it reaches its own
    # interlocks (which fail for lack of facts, proving the gate ran normally)
    with pytest.raises(Exception) as excinfo:
        run(runtime, "pump.start", {})
    assert not isinstance(excinfo.value, RollbackFrozenError)


def test_withdrawing_a_cross_line_conclusion_freezes_the_whole_plant(runtime: PlantRuntime) -> None:
    run_full_cycle(runtime)  # delivery ends at "delivered"
    result = run(runtime, "rollback.begin", {"line": "delivery", "reason": "truck rejected"})
    assert result["rollback"]["scope"] == "full"
    assert result["rollback"]["to_phase"] == "idle"
    for command in ("agg.weigh", "mix.start", "pump.start"):
        with pytest.raises(RollbackFrozenError):
            run(runtime, command, {})
    # audit and the realignment path stay reachable while frozen
    run(runtime, "audit.note", {"name": "observing.freeze"})


def test_rollback_then_realign_ordering_is_enforced(runtime: PlantRuntime) -> None:
    advance_to_mixed(runtime)
    run(runtime, "rollback.begin", {"line": "mix", "reason": "x"})
    # cannot nest rollbacks
    with pytest.raises(OrderViolationError):
        run(runtime, "rollback.begin", {"line": "batch", "reason": "y"})
    # cannot realign a different line
    with pytest.raises(ValidationError):
        run(runtime, "rollback.realign", {"line": "batch", "confirmed_by": "lead"})
    # realignment must name a confirmer
    with pytest.raises(ValidationError):
        run(runtime, "rollback.realign", {"line": "mix"})
    run(runtime, "rollback.realign", {"line": "mix", "confirmed_by": "lead-7"})
    assert runtime.rollbacks.is_open() is False


def test_realign_clears_the_freeze_and_records_the_confirmer(runtime: PlantRuntime) -> None:
    advance_to_mixed(runtime)
    run(runtime, "rollback.begin", {"line": "mix", "reason": "x"})
    result = run(
        runtime,
        "rollback.realign",
        {"line": "mix", "confirmed_by": "lead-7", "note": "re-zeroed probe"},
    )
    episode = result["rollback"]
    assert episode["confirmed_by"] == "lead-7"
    assert episode["realign_note"] == "re-zeroed probe"
    assert episode["realigned_tick"] is not None


def test_rollback_is_append_only_and_keeps_prior_conclusions(runtime: PlantRuntime) -> None:
    advance_to_mixed(runtime)
    before = len(runtime.machines["mix"].history())
    run(runtime, "rollback.begin", {"line": "mix", "reason": "x"})
    run(runtime, "rollback.realign", {"line": "mix", "confirmed_by": "lead"})
    history = runtime.machines["mix"].history()
    # the machine only ever appended: prior stages are all still present
    assert len(history) == before + 1
    assert any(step.reason.startswith("rollback:") for step in history)
    # no business record was tombstoned; rollback only added records
    assert not any(record.is_tombstone for record in runtime.store.committed_records())
    kinds = {record.kind for record in runtime.store.committed_records()}
    assert {"rollback.started", "rollback.realigned"} <= kinds


def test_a_freeze_keeps_blocking_across_a_restart(tmp_path: Path) -> None:
    config = StationConfig(web_dir="web", data_dir=tmp_path)
    first = PlantRuntime(config, storage="file")
    first.bootstrap()
    run_full_cycle(first)
    run(first, "rollback.begin", {"line": "delivery", "reason": "truck rejected"})
    for command in ("pump.start", "agg.weigh", "mix.start"):
        with pytest.raises(RollbackFrozenError):
            run(first, command, {})

    second = PlantRuntime(StationConfig(web_dir="web", data_dir=tmp_path), storage="file")
    second.bootstrap()
    assert second.rollbacks.is_open("delivery") is True
    # the freeze still blocks after the restart (it was rebuilt from the stream)
    with pytest.raises(RollbackFrozenError):
        run(second, "sampler.draw", {"litres": 12})
    open_episode = second.rollbacks.open_episode("delivery")
    assert open_episode is not None
    assert [item.action for item in open_episode.blocked] == [
        "pump.start",
        "agg.weigh",
        "mix.start",
        "sampler.draw",
    ]
    run(second, "rollback.realign", {"line": "delivery", "confirmed_by": "lead"})
    assert second.rollbacks.is_open() is False


def test_cannot_roll_back_from_an_intermediate_stage(runtime: PlantRuntime) -> None:
    # a fresh plant sits at idle (the first stable stage): nothing to return to
    with pytest.raises(OrderViolationError):
        run(runtime, "rollback.begin", {"line": "delivery", "reason": "too early"})
