"""Stage rollback: policy, windows, guard and the fixed target rule."""

from __future__ import annotations

import pytest

from mixplant.errors import (
    OrderViolationError,
    RollbackBlockedError,
    RollbackWindowError,
    ValidationError,
)
from mixplant.statemachine.gate import PreGate
from mixplant.statemachine.guard import RollbackGuard
from mixplant.statemachine.interlock import InterlockEngine
from mixplant.statemachine.machine import SequenceMachine
from mixplant.statemachine.rollback import (
    ROLLBACK_BLOCKED,
    ROLLBACK_CONFIRMED,
    ROLLBACK_OPENED,
)
from mixplant.statemachine.rollback_policy import (
    BLOCK_ALL,
    BLOCK_RELATED,
    policy_for,
    previous_stable_phase,
    stable_phases,
)


def test_stable_targets_are_fixed_and_skip_transient_stages() -> None:
    order = SequenceMachine("batch").order()
    assert previous_stable_phase("batch", order, "idle") is None
    assert previous_stable_phase("batch", order, "recipe_selected") == "idle"
    assert previous_stable_phase("batch", order, "weighing") == "recipe_selected"
    assert previous_stable_phase("batch", order, "weighed") == "recipe_selected"
    assert previous_stable_phase("batch", order, "dropped") == "weighed"

    delivery = SequenceMachine("delivery").order()
    # pumping is transient: it rolls back to the primed stable stage
    assert previous_stable_phase("delivery", delivery, "pumping") == "primed"
    assert previous_stable_phase("delivery", delivery, "delivered") == "primed"

    mix = SequenceMachine("mix").order()
    assert previous_stable_phase("mix", mix, "water_in") == "idle"
    assert previous_stable_phase("mix", mix, "mixing") == "idle"
    assert previous_stable_phase("mix", mix, "mixed") == "mixing"


def test_every_rollback_target_is_a_declared_stable_stage() -> None:
    assert stable_phases("batch") == ("idle", "recipe_selected", "weighed", "dropped")
    with pytest.raises(ValidationError):
        policy_for("batch", "weighing")
    with pytest.raises(ValidationError):
        previous_stable_phase("unknown", ("idle",), "idle")


def test_scope_is_all_for_idle_and_related_otherwise() -> None:
    assert policy_for("mix", "idle").scope == BLOCK_ALL
    assert policy_for("mix", "mixing").scope == BLOCK_RELATED
    assert policy_for("batch", "weighed").scope == BLOCK_RELATED


def test_related_policy_blocks_only_related_actions_of_the_line() -> None:
    policy = policy_for("batch", "recipe_selected")
    assert policy.blocks("agg.weigh")
    assert policy.blocks("batch.declare")
    # related scope leaves unrelated actions of the same line open
    assert not policy.blocks("agg.calibrate")
    # and never touches another line
    assert not policy.blocks("mix.start")


def test_all_policy_blocks_every_action_of_the_line_only() -> None:
    policy = policy_for("mix", "idle")
    assert policy.blocks("mix.start")
    assert policy.blocks("cement.load_silo")
    assert policy.blocks("chute.open")
    assert not policy.blocks("agg.weigh")
    assert not policy.blocks("pump.start")


def test_window_requires_a_reason_and_refuses_nesting() -> None:
    guard = RollbackGuard(("batch",))
    with pytest.raises(ValidationError):
        guard.open_window(
            "batch", target_phase="recipe_selected", from_phase="weighed", reason="  ", tick=1,
            rollback_id="rb-1",
        )
    guard.open_window(
        "batch", target_phase="recipe_selected", from_phase="weighed", reason="redo", tick=1,
        rollback_id="rb-1",
    )
    with pytest.raises(RollbackWindowError):
        guard.open_window(
            "batch", target_phase="idle", from_phase="recipe_selected", reason="again", tick=2,
            rollback_id="rb-2",
        )


def test_window_confirms_only_after_opening_and_with_a_reason() -> None:
    guard = RollbackGuard(("batch",))
    with pytest.raises(RollbackWindowError):
        guard.window("batch").confirm(
            reason="x", tick=2, confirmer="op", realigned_phase="recipe_selected",
            rollback_id="rb-1",
        )
    guard.open_window(
        "batch", target_phase="recipe_selected", from_phase="weighed", reason="redo", tick=1,
        rollback_id="rb-1",
    )
    with pytest.raises(ValidationError):
        guard.window("batch").confirm(
            reason=" ", tick=2, confirmer="op", realigned_phase="recipe_selected",
            rollback_id="rb-1",
        )
    guard.window("batch").confirm(
        reason="realigned", tick=3, confirmer="lead", realigned_phase="recipe_selected",
        rollback_id="rb-1",
    )
    assert not guard.window("batch").open
    names = [event.name for event in guard.window("batch").history()]
    assert names == [ROLLBACK_OPENED, ROLLBACK_CONFIRMED]


def test_blocked_actions_are_appended_in_arrival_order() -> None:
    guard = RollbackGuard(("batch",))
    guard.open_window(
        "batch", target_phase="recipe_selected", from_phase="weighed", reason="redo", tick=1,
        rollback_id="rb-1",
    )
    for action, tick in (("agg.weigh", 2), ("agg.drop", 3), ("agg.release", 4)):
        assert guard.record_block(action, tick=tick) is not None
        with pytest.raises(RollbackBlockedError):
            guard.check(action)
    blocked = [
        str(event.detail["action"])
        for event in guard.window("batch").history()
        if event.name == ROLLBACK_BLOCKED
    ]
    assert blocked == ["agg.weigh", "agg.drop", "agg.release"]


def test_pregate_denies_through_the_open_window_before_stage_rules() -> None:
    guard = RollbackGuard(("batch",))
    gate = PreGate(InterlockEngine(()), guard)
    machine = SequenceMachine("batch")
    machine.advance("recipe_selected", tick=1, reason="x")
    guard.open_window(
        "batch", target_phase="recipe_selected", from_phase="weighed", reason="redo", tick=2,
        rollback_id="rb-1",
    )
    result = gate.evaluate("agg.weigh", facts={}, machine=machine, required_phase="weighed", tick=3)
    assert result.allowed is False
    assert result.decision.blocking[0].startswith("rollback:")
    with pytest.raises(RollbackBlockedError):
        gate.require("agg.weigh", facts={}, machine=machine, required_phase="weighed", tick=4)
    # the gate appended both denials in order
    blocked = [
        str(event.detail["action"])
        for event in guard.window("batch").history()
        if event.name == ROLLBACK_BLOCKED
    ]
    assert blocked == ["agg.weigh", "agg.weigh"]


def test_rollback_window_does_not_change_stage_conclusions_by_itself() -> None:
    machine = SequenceMachine("batch")
    machine.advance("recipe_selected", tick=1, reason="sel")
    machine.advance("weighing", tick=2, reason="weigh")
    machine.advance("weighed", tick=3, reason="durable")
    before = tuple(machine.history())
    guard = RollbackGuard(("batch",))
    guard.open_window(
        "batch", target_phase="recipe_selected", from_phase="weighed", reason="redo", tick=4,
        rollback_id="rb-1",
    )
    # opening a window alone leaves the machine and its conclusions untouched
    assert tuple(machine.history()) == before
    # and a refused machine rewind still cannot move forwards
    with pytest.raises(OrderViolationError):
        machine.rewind_to("dropped", tick=5, reason="forward")
