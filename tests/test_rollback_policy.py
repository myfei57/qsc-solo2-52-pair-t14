"""Controlled rollback: target resolution and freeze breadth."""

from __future__ import annotations

import pytest

from mixplant.errors import ValidationError
from mixplant.statemachine.rollback_policy import (
    SCOPE_FULL,
    SCOPE_RELATED,
    describe_line,
    is_stable,
    previous_stable,
    stable_stages,
    withdraw_scope,
)


def test_stable_stages_are_declared_in_line_order() -> None:
    assert tuple(stable_stages("batch")) == ("idle", "weighed", "dropped")
    assert tuple(stable_stages("mix")) == ("idle", "mixed", "discharged")
    assert tuple(stable_stages("delivery")) == ("idle", "delivered")
    assert is_stable("mix", "mixed") is True
    assert is_stable("mix", "mixing") is False


def test_rollback_target_is_the_previous_stable_stage_not_one_step() -> None:
    # From "dropped" the previous stable is "weighed", regardless of the
    # intermediate stages a one-step rewind would have landed on.
    assert previous_stable("batch", "dropped") == "weighed"
    assert previous_stable("batch", "weighed") == "idle"
    assert previous_stable("delivery", "delivered") == "idle"


def test_first_stable_stage_has_nowhere_to_return_to() -> None:
    assert previous_stable("mix", "idle") is None


def test_a_rollback_cannot_start_from_an_intermediate_stage() -> None:
    with pytest.raises(ValidationError):
        previous_stable("mix", "mixing")


def test_freeze_breadth_follows_the_withdrawn_conclusion() -> None:
    assert withdraw_scope("batch", "weighed") == SCOPE_RELATED
    assert withdraw_scope("batch", "dropped") == SCOPE_FULL
    assert withdraw_scope("delivery", "delivered") == SCOPE_FULL
    assert withdraw_scope("mix", "idle") == SCOPE_RELATED


def test_unknown_line_is_rejected() -> None:
    with pytest.raises(ValidationError):
        stable_stages("plant")
    policy = describe_line("delivery")
    assert tuple(policy["stable"]) == ("idle", "delivered")
    assert policy["withdraw_freeze"]["delivered"] == SCOPE_FULL
