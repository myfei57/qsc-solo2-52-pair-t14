"""End to end rollback windows through the runtime and the console."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mixplant.config import StationConfig
from mixplant.errors import RollbackBlockedError, RollbackWindowError, ValidationError
from mixplant.rollback import (
    ROLLBACK_BLOCK_KIND,
    ROLLBACK_CONFIRM_KIND,
    ROLLBACK_KIND,
)
from mixplant.runtime import PlantRuntime
from mixplant.console.router import Request
from tests.support import (
    advance_to_mixed,
    advance_to_mixing,
    batch_and_weigh,
    run,
)


def test_rollback_lands_on_the_previous_stable_stage_and_blocks_related_actions(runtime: PlantRuntime) -> None:
    batch_and_weigh(runtime)
    assert runtime.machines["batch"].phase == "weighed"

    result = runtime.rollback_phase("batch", reason="lab ticket disagrees with the weight")
    assert result["target_phase"] == "recipe_selected"
    assert runtime.machines["batch"].phase == "recipe_selected"
    assert result["window"]["scope"] == "related"

    for command, payload in (
        ("agg.weigh", {"bin": "bin-1", "actual_kg": 1}),
        ("agg.drop", {}),
        ("agg.release", {"residual_kg": 1}),
    ):
        with pytest.raises(RollbackBlockedError):
            run(runtime, command, payload)

    # unrelated actions of the same line, and other lines, keep working
    run(runtime, "agg.calibrate", {"slope": 1.0, "offset": 0.0})
    run(runtime, "cement.load_silo", {"silo": "silo-1", "level_kg": 1000})

    blocked = runtime.rollback_guard.window("batch").describe()["blocked_actions"]
    assert blocked == ["agg.weigh", "agg.drop", "agg.release"]


def test_rollback_requires_a_reason(runtime: PlantRuntime) -> None:
    batch_and_weigh(runtime)
    with pytest.raises(ValidationError):
        runtime.rollback_phase("batch", reason="  ")


def test_nested_rollback_is_refused_without_moving_the_machine(runtime: PlantRuntime) -> None:
    batch_and_weigh(runtime)
    runtime.rollback_phase("batch", reason="first")
    with pytest.raises(RollbackWindowError):
        runtime.rollback_phase("batch", reason="second")
    assert runtime.machines["batch"].phase == "recipe_selected"


def test_first_stage_has_no_rollback_target(runtime: PlantRuntime) -> None:
    with pytest.raises(RollbackWindowError):
        runtime.rollback_phase("delivery", reason="nothing to return to")


def test_full_block_scope_closes_the_whole_line_but_not_other_lines(runtime: PlantRuntime) -> None:
    advance_to_mixing(runtime)
    result = runtime.rollback_phase("mix", reason="wrong water amount")
    assert result["target_phase"] == "idle"
    assert result["window"]["scope"] == "all"

    for command, payload in (
        ("mix.start", {}),
        ("mix.feed", {"component": "water"}),
        ("cement.open_gate", {"silo": "silo-1", "target_kg": 1}),
        ("chute.arrive", {"truck_id": "T-9"}),
    ):
        with pytest.raises(RollbackBlockedError):
            run(runtime, command, payload)

    # the delivery line is independent
    run(runtime, "water.prime", {"litres": 60})
    run(runtime, "pump.prime")


def test_confirmation_closes_the_window_and_releases_the_gate(runtime: PlantRuntime) -> None:
    batch_and_weigh(runtime)
    runtime.rollback_phase("batch", reason="redo weighing")
    with pytest.raises(RollbackBlockedError):
        run(runtime, "agg.drop")

    with pytest.raises(ValidationError):
        runtime.confirm_realignment("batch", reason=" ")

    result = runtime.confirm_realignment("batch", reason="scale recalibrated", confirmer="lead")
    assert result["realigned_phase"] == "recipe_selected"
    assert runtime.rollback_guard.window("batch").open is False

    # after confirmation the gate evaluates normally again (no rollback denial)
    try:
        run(runtime, "agg.weigh", {"bin": "bin-1", "actual_kg": 1})
    except RollbackBlockedError as exc:  # pragma: no cover - must never be a rollback block
        raise AssertionError("still blocked by the closed window") from exc


def test_confirm_without_an_open_window_is_refused(runtime: PlantRuntime) -> None:
    with pytest.raises(RollbackWindowError):
        runtime.confirm_realignment("batch", reason="nothing open")


def test_stage_conclusions_remain_an_append_only_chain(runtime: PlantRuntime) -> None:
    batch_and_weigh(runtime)
    runtime.rollback_phase("batch", reason="redo")
    runtime.confirm_realignment("batch", reason="fixed")

    phases = [step.phase for step in runtime.machines["batch"].history()]
    assert phases == [
        "idle",
        "recipe_selected",
        "weighing",
        "weighed",
        "recipe_selected",
    ]
    assert runtime.machines["batch"].last_step.reason.startswith("rollback:")
    # the durable stream retains every prior conclusion as append-only records
    durable_phases = [
        str(record.payload["phase"])
        for record in runtime.store.visible()
        if record.kind == "batch.phase"
    ]
    assert durable_phases == [
        "recipe_selected",
        "weighing",
        "weighed",
        "recipe_selected",
    ]


def test_rollback_and_blocks_and_confirmation_are_audited_in_order(runtime: PlantRuntime) -> None:
    batch_and_weigh(runtime)
    runtime.rollback_phase("batch", reason="redo weighing")
    for command in ("agg.weigh", "agg.drop"):
        try:
            run(runtime, command, {} if command == "agg.drop" else {"bin": "bin-1", "actual_kg": 1})
        except RollbackBlockedError:
            pass
    runtime.confirm_realignment("batch", reason="realigned")

    names = [entry.name for entry in runtime.audit.ledger.entries()]
    window = names[
        names.index("rollback.opened") : names.index("rollback.confirmed") + 1
    ]
    assert window == [
        "rollback.opened",
        "rollback.action_blocked",
        "rollback.action_blocked",
        "rollback.confirmed",
    ]

    kinds = [record.kind for record in runtime.store.visible() if record.kind.startswith("rollback.")]
    assert kinds.count(ROLLBACK_KIND) == 1
    assert kinds.count(ROLLBACK_BLOCK_KIND) == 2
    assert kinds.count(ROLLBACK_CONFIRM_KIND) == 1

    # the durable phase conclusion for the rewind is also appended, never erased
    phase_records = [record for record in runtime.store.visible() if record.kind == "batch.phase"]
    assert [record.payload["phase"] for record in phase_records][-1] == "recipe_selected"


def test_state_snapshot_exposes_the_window_and_history(runtime: PlantRuntime) -> None:
    batch_and_weigh(runtime)
    runtime.rollback_phase("batch", reason="redo")
    state = runtime.state()
    window = state["lines"]["batch"]["rollback_window"]
    assert window["target_phase"] == "recipe_selected"
    assert window["reason"] == "redo"
    history_names = [event["name"] for event in state["lines"]["batch"]["rollback_history"]]
    assert history_names == ["rollback.opened"]
    assert any(line["line"] == "batch" for line in state["rollback"]["open_windows"]["open"])


def test_open_window_survives_restart_with_its_block_order(tmp_path) -> None:
    config = StationConfig(data_dir=tmp_path / "state", web_dir=Path("web"))
    runtime = PlantRuntime(config, storage="file")
    runtime.bootstrap()
    batch_and_weigh(runtime)
    runtime.rollback_phase("batch", reason="weight mismatch")
    for command, payload in (
        ("agg.weigh", {"bin": "bin-1", "actual_kg": 1}),
        ("agg.drop", {}),
    ):
        with pytest.raises(RollbackBlockedError):
            run(runtime, command, payload)
    runtime.close()

    restarted = PlantRuntime(config, storage="file")
    restarted.bootstrap()
    window = restarted.rollback_guard.window("batch")
    assert window.open is True
    assert window.describe()["target_phase"] == "recipe_selected"
    blocked = [
        event.detail["action"]
        for event in window.history()
        if event.name == "rollback.action_blocked"
    ]
    assert blocked == ["agg.weigh", "agg.drop"]
    with pytest.raises(RollbackBlockedError):
        run(restarted, "agg.persist")

    confirmed = restarted.confirm_realignment("batch", reason="re-weighed")
    assert confirmed["realigned_phase"] == "recipe_selected"
    restarted.close()


def test_console_routes_drive_the_window(runtime: PlantRuntime) -> None:
    batch_and_weigh(runtime)
    from mixplant.console.run import build_server

    server = build_server(runtime)

    def request(method: str, path: str, body: dict | None = None):
        return server.handle(Request(method=method, path=path, body=body or {}))

    missing = request("POST", "/api/lines/batch/rollback", {})
    assert missing.status == 400
    assert json.loads(missing.text)["code"] == "validation_error"

    opened = request("POST", "/api/lines/batch/rollback", {"reason": "ticket mismatch"})
    assert opened.status == 200
    assert json.loads(opened.text)["target_phase"] == "recipe_selected"

    blocked = request("POST", "/api/agg/drop", {})
    assert blocked.status == 409
    payload = json.loads(blocked.text)
    assert payload["code"] == "rollback_blocked"

    status = request("GET", "/api/rollbacks", {})
    line = json.loads(status.text)["lines"]["batch"]
    assert line["window"]["blocked_actions"] == ["agg.drop"]

    confirmed = request(
        "POST", "/api/lines/batch/realign", {"reason": "fixed", "confirmer": "lead"}
    )
    assert confirmed.status == 200
    assert json.loads(confirmed.text)["open"] is False
