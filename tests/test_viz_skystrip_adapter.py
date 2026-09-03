"""Skystrip is one production-backed fixture for the generic viz contract."""

from __future__ import annotations

import pytest

from busybar_viz.adapters.skystrip import SkystripAdapter, _find_checkout, _skystrip
from busybar_viz.analysis import analyze, required_checks_pass
from busybar_viz.models import CheckStatus, EvidenceLevel, InputEvent, RenderRequest


def _render(scenario: str, parameters=None, inputs=()):
    return SkystripAdapter().render(
        RenderRequest.from_values(scenario, parameters, inputs)
    )


def test_checkout_discovery_walks_up_from_an_apps_subdirectory(tmp_path):
    checkout = tmp_path / "busybar-lab"
    nested = checkout / "apps" / "nested"
    nested.mkdir(parents=True)
    (checkout / "AGENTS.md").write_text("# fixture\n")
    (checkout / "apps" / "skystrip.py").write_text("# fixture\n")

    assert _find_checkout(nested) == checkout


def test_checkout_discovery_does_not_fall_back_to_the_installed_package(tmp_path):
    with pytest.raises(ValueError, match="inside a BUSY Bar Lab checkout"):
        _find_checkout(tmp_path)


def test_registered_scenarios_expose_truthful_near_and_distant_defaults():
    specs = {spec.id: spec for spec in SkystripAdapter().scenarios()}

    assert set(specs) == {
        "skystrip/lightning-near",
        "skystrip/lightning-distant",
        "skystrip/thunder-loop",
        # The status clock had no registered coverage at all until it shipped
        # below the panel's contrast floor. See tests/test_viz_device_laws.py.
        "skystrip/status-clock",
    }
    assert specs["skystrip/lightning-near"].controls[0].default == 5.0
    assert specs["skystrip/lightning-distant"].controls[0].default == 40.0
    assert all(spec.adapter == "skystrip" for spec in specs.values())


def test_near_and_distant_lightning_use_the_same_generic_display_contract():
    near = _render("skystrip/lightning-near")
    distant = _render("skystrip/lightning-distant")

    for segment in (near, distant):
        assert segment.evidence_level is EvidenceLevel.RENDERER_VERIFIED
        assert len(segment.displays) == 1
        track = segment.displays[0]
        assert (
            track.id, track.size, len(track.frames), track.fps, track.duration_us,
        ) == (
            "front", (72, 16), 24, 12, 2_000_000,
        )
        results = {result.id: result for result in analyze(segment)}
        assert results["native-lease"].status is CheckStatus.PASS
        assert results["post-flash-motion"].status is CheckStatus.PASS
        assert required_checks_pass(tuple(results.values()))

    assert [signal.kind for signal in near.signals] == ["top_led.pulse"]
    assert distant.signals == ()


def test_fault_injection_proves_the_audit_rejects_a_full_screen_white_wash():
    segment = _render("skystrip/lightning-near", {"fault": "white_wash"})
    results = {result.id: result for result in analyze(segment)}

    assert results["near-white"].status is CheckStatus.FAIL
    assert results["global-luminance"].status is CheckStatus.FAIL
    assert results["foreground-stable"].status is CheckStatus.FAIL
    assert not required_checks_pass(tuple(results.values()))


def test_thunder_fixture_matches_the_production_rain_loop_clock():
    segment = _render("skystrip/thunder-loop")
    track = segment.displays[0]

    # Production doubles both frame count and playback rate for rain, retaining
    # the same eight-second loop without making precipitation jump six rows.
    assert (len(track.frames), track.fps, track.duration_us) == (
        80, 10, 8_000_000,
    )
    assert segment.signals == ()
    assert required_checks_pass(analyze(segment))


def test_adapter_output_does_not_depend_on_cached_renderer_globals(
    monkeypatch,
):
    module = _skystrip()
    preexisting_tz = module.ZoneInfo("America/Los_Angeles")
    # London's published city-centre reference point is a public fixture.
    preexisting_observer = module.Observer(latitude=51.5074, longitude=-0.1278)
    monkeypatch.setattr(module, "TZ", preexisting_tz)
    monkeypatch.setattr(module, "OBSERVER", preexisting_observer)
    monkeypatch.setattr(module, "UNITS", "c")

    first = _render("skystrip/lightning-near").displays[0]

    # The adapter fences and restores globals rather than permanently mutating
    # an app module that another test or tool may already be using.
    assert module.TZ is preexisting_tz
    assert module.OBSERVER is preexisting_observer
    assert module.UNITS == "c"
    second = _render("skystrip/lightning-near").displays[0]
    assert [frame.tobytes() for frame in first.frames] == [
        frame.tobytes() for frame in second.frames
    ]


@pytest.mark.parametrize(
    "value",
    (-1, 61, True, "storm", float("nan"), float("inf")),
)
def test_lightning_distance_rejects_values_outside_its_declared_control(value):
    with pytest.raises(ValueError, match="distance_km"):
        _render("skystrip/lightning-near", {"distance_km": value})


def test_skystrip_fixture_rejects_unknown_controls_and_unimplemented_inputs():
    with pytest.raises(ValueError, match="unknown Skystrip controls"):
        _render("skystrip/lightning-near", {"another_app_setting": 1})
    with pytest.raises(ValueError, match="do not accept input events"):
        _render(
            "skystrip/lightning-near",
            inputs=(InputEvent(0, "button.press", "ok"),),
        )
