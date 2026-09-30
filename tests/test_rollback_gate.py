"""The pre gate treats an open rollback as its first refusal."""

from __future__ import annotations

import pytest

from mixplant.errors import RollbackFrozenError
from mixplant.interlocks import build_engine
from mixplant.statemachine.gate import PreGate
from mixplant.statemachine.machine import SequenceMachine
from mixplant.statemachine.rollback import RollbackRegistry
from mixplant.statemachine.rollback_policy import SCOPE_FULL, SCOPE_RELATED


def _gate_with_freeze(line="mix", scope=SCOPE_RELATED, from_phase="mixed", to_phase="idle"):
    registry = RollbackRegistry()
    registry.begin_episode(
        rollback_id="RB-0001",
        line=line,
        from_phase=from_phase,
        to_phase=to_phase,
        scope=scope,
        reason="probe",
        tick=1,
        seq=1,
    )
    gate = PreGate(build_engine(), registry)
    return gate, registry


def test_related_freeze_blocks_the_line_even_with_facts_present() -> None:
    gate, _ = _gate_with_freeze(line="mix")
    # all interlock facts true; the freeze still wins and is checked first
    facts = {
        "mix.running": True,
        "water.dispensed": True,
        "cement.fed": True,
        "agg.hopper_released": True,
    }
    result = gate.evaluate("mix.stop", facts=facts, machine=SequenceMachine("mix"), required_phase="mixed")
    assert result.allowed is False
    assert result.decision.blocking[0] == f"rollback:{SCOPE_RELATED}"


def test_related_freeze_lets_another_line_through_to_its_interlocks() -> None:
    gate, _ = _gate_with_freeze(line="mix")
    # agg.weigh is on the batch line; with no facts it is interlock-blocked,
    # not rollback-blocked, proving the freeze did not cover it
    result = gate.evaluate("agg.weigh", facts={})
    assert result.allowed is False
    assert not result.decision.blocking[0].startswith("rollback:")


def test_full_freeze_blocks_unrelated_lines() -> None:
    gate, _ = _gate_with_freeze(line="delivery", scope=SCOPE_FULL, from_phase="delivered")
    result = gate.evaluate("agg.weigh", facts={"recipe.selected": True, "agg.bin_mapped": True})
    assert result.allowed is False
    assert result.decision.blocking[0] == f"rollback:{SCOPE_FULL}"


def test_require_raises_the_dedicated_freeze_error() -> None:
    gate, registry = _gate_with_freeze(line="mix")
    with pytest.raises(RollbackFrozenError) as excinfo:
        gate.require("mix.stop", facts={"mix.running": True, "agg.hopper_released": True})
    assert excinfo.value.context["line"] == "mix"
    assert excinfo.value.context["scope"] == SCOPE_RELATED
    # the refusal is observable for the audit layer through the registry
    assert registry.episode_for_action("mix.stop") is not None


def test_gate_without_a_registry_behaves_as_before() -> None:
    gate = PreGate(build_engine())
    result = gate.evaluate("nothing.here", facts={})
    assert result.allowed is True
