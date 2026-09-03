"""Versioned model payloads stay portable across CLI, HTTP, and harnesses."""

from __future__ import annotations

import pytest

from busybar_viz.models import (
    RENDER_REQUEST_SCHEMA,
    ControlSpec,
    EvidenceLevel,
    InputEvent,
    InputSpec,
    RenderRequest,
    ScenarioSpec,
)
from busybar_viz.profiles import BACK, FRONT, profile_for
from busybar_viz.registry import adapter_for_scenario, adapters, scenarios


def test_scenario_spec_serializes_controls_inputs_and_named_displays():
    spec = ScenarioSpec(
        "fixture/replay",
        "Replay",
        "A generic replay fixture.",
        "fixture",
        controls=(ControlSpec(
            "level", "Level", "number", 2, minimum=0, maximum=8,
        ),),
        inputs=(InputSpec(
            "mode", "Mode", "button.press", (True,),
        ),),
        expected_displays=("front", "back"),
    )

    assert spec.as_dict() == {
        "schema": "busybar.scenario/v1",
        "id": "fixture/replay",
        "title": "Replay",
        "description": "A generic replay fixture.",
        "adapter": "fixture",
        "controls": [{
            "id": "level",
            "label": "Level",
            "kind": "number",
            "default": 2,
            "choices": [],
            "minimum": 0,
            "maximum": 8,
        }],
        "inputs": [{
            "id": "mode",
            "label": "Mode",
            "kind": "button.press",
            "values": [True],
        }],
        "expected_displays": ["front", "back"],
    }


def test_render_request_has_a_lossless_versioned_round_trip():
    request = RenderRequest.from_values(
        "fixture/replay",
        {"level": 3},
        (InputEvent(250_000, "encoder.delta", "level", -1),),
    )

    payload = request.as_dict()
    rebuilt = RenderRequest.from_dict(payload)

    assert payload["schema"] == RENDER_REQUEST_SCHEMA
    assert rebuilt.scenario_id == request.scenario_id
    assert rebuilt.parameters == request.parameters
    assert rebuilt.inputs == request.inputs


def test_render_request_deeply_detaches_and_freezes_nested_json_values():
    parameters = {"view": {"labels": ["A", "B"]}}
    event_value = {"steps": [1, 2]}
    request = RenderRequest.from_values(
        "fixture/replay",
        parameters,
        (InputEvent(1, "encoder.delta", "level", event_value),),
    )

    parameters["view"]["labels"].append("MUTATED")
    event_value["steps"].append(3)

    assert request.as_dict()["parameters"] == {
        "view": {"labels": ["A", "B"]},
    }
    assert request.inputs[0].as_dict()["value"] == {"steps": [1, 2]}
    with pytest.raises(TypeError, match="immutable"):
        request.parameters["view"]["labels"].append("NOPE")
    with pytest.raises(TypeError, match="immutable"):
        request.inputs[0].value["steps"] = []

    plain = request.as_dict()
    plain["parameters"]["view"]["labels"].append("LOCAL")
    plain["inputs"][0]["value"]["steps"].append(9)
    assert request.as_dict()["parameters"] == {
        "view": {"labels": ["A", "B"]},
    }
    assert request.inputs[0].as_dict()["value"] == {"steps": [1, 2]}


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        ({}, "scenario_id"),
        ({"schema": "future", "scenario_id": "x"}, "schema"),
        ({"scenario_id": "x", "extra": True}, "unknown"),
        ({"scenario_id": "x", "parameters": []}, "parameters"),
        ({"scenario_id": "x", "inputs": "press"}, "inputs"),
        ({
            "scenario_id": "x",
            "inputs": [{"t_us": 1.5, "kind": "press", "control": "ok"}],
        }, "integer"),
    ),
)
def test_render_request_rejects_ambiguous_or_future_payloads(payload, message):
    with pytest.raises(ValueError, match=message):
        RenderRequest.from_dict(payload)


def test_display_profiles_are_named_device_capabilities_not_app_types():
    assert profile_for("front") is FRONT
    assert profile_for("back") is BACK
    assert FRONT.size == (72, 16)
    assert BACK.size == (160, 80)
    with pytest.raises(ValueError, match="unknown BUSY Bar display"):
        profile_for("skystrip")


def test_user_facing_evidence_levels_match_repository_confidence_language():
    assert [level.value for level in EvidenceLevel] == [
        "renderer-verified",
        "gap-previewed",
        "framebuffer-captured",
        "hardware-observed",
    ]


def test_closed_registry_has_unique_generic_scenario_ids_and_matching_adapters():
    registered_adapters = {adapter.id: adapter for adapter in adapters()}
    registered_scenarios = scenarios()
    ids = [spec.id for spec in registered_scenarios]

    assert len(ids) == len(set(ids))
    assert "conformance/dual-display-input-replay" in ids
    assert "skystrip/lightning-near" in ids
    assert all(spec.adapter in registered_adapters for spec in registered_scenarios)
    for spec in registered_scenarios:
        assert adapter_for_scenario(spec.id).id == spec.adapter
    with pytest.raises(KeyError, match="unknown scenario"):
        adapter_for_scenario("not/registered")
