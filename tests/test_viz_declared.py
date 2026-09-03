"""apps.toml viz declarations register scenarios as data, not adapter code."""

from __future__ import annotations

import sys
import textwrap

import pytest

from busybar_viz import declared
from busybar_viz.analysis import analyze, required_checks_pass
from busybar_viz.declared import DeclaredAdapter, load_declarations
from busybar_viz.models import Confidence, EvidenceLevel, RenderRequest

FAKE_APP = textwrap.dedent(
    '''
    from PIL import Image


    def render_visual():
        frame = Image.new("RGB", (72, 16), (100, 100, 100))
        for x in range(4, 10):
            frame.putpixel((x, 5), (120, 120, 120))
        return {"front": ([frame], 1)}


    def render_alt():
        return {"front": ([Image.new("RGB", (72, 16), (0, 80, 0))], 1)}
    '''
)

MANIFEST = textwrap.dedent(
    '''
    [fake]
    kind = "foreground"
    entrypoint = "apps/fake.py"
    description = "Deterministic fixture app"

    [fake.viz]
    renderer = "apps.fake:render_visual"
    displays = ["front"]
    description = "Fixture default scenario"

    [fake.viz.regions.label]
    rect = [0, 0, 20, 10]
    ink = ["787878"]
    max_isolated = 1
    contrast_mode = "luminance_or_channel"

    [fake.viz.scenarios.alt]
    renderer = "apps.fake:render_alt"
    description = "A second declared view"
    '''
)


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    (tmp_path / "AGENTS.md").write_text("# fixture checkout\n")
    (tmp_path / "apps").mkdir()
    (tmp_path / "apps" / "fake.py").write_text(FAKE_APP)
    (tmp_path / "apps.toml").write_text(MANIFEST)
    monkeypatch.setattr(declared, "find_checkout", lambda start=None: tmp_path)
    try:
        yield tmp_path
    finally:
        sys.modules.pop("apps.fake", None)
        if str(tmp_path) in sys.path:
            sys.path.remove(str(tmp_path))


def test_declaration_parses_normalizes_and_lists_scenarios(checkout):
    declarations = load_declarations(checkout)
    assert [item.scenario_id for item in declarations] == [
        "fake/default", "fake/alt",
    ]
    declaration = declarations[0]
    assert declaration.renderer_path == "apps/fake.py"
    assert declaration.regions[0].inks == ("#787878",)
    assert declaration.regions[0].max_isolated == 1
    assert declaration.regions[0].contrast_mode == "luminance_or_channel"
    assert declarations[1].function == "render_alt"
    assert declarations[1].regions == ()

    specs = DeclaredAdapter().scenarios()
    assert [spec.id for spec in specs] == ["fake/default", "fake/alt"]
    assert specs[0].adapter == "declared"
    assert specs[0].expected_displays == ("front",)
    assert specs[0].description == "Fixture default scenario"


def test_named_scenarios_render_and_default_is_optional(checkout):
    segment = DeclaredAdapter().render(RenderRequest.from_values("fake/alt"))
    assert segment.displays[0].frames[0].getpixel((0, 0)) == (0, 80, 0)

    (checkout / "apps.toml").write_text(textwrap.dedent(
        '''
        [fake.viz.scenarios.only]
        renderer = "apps.fake:render_alt"
        '''
    ))
    assert [d.scenario_id for d in load_declarations(checkout)] == ["fake/only"]


def test_named_scenario_declarations_fail_closed(tmp_path):
    (tmp_path / "apps.toml").write_text(
        "[fake.viz]\nrenderer = 'apps.fake:render_visual'\n"
        "[fake.viz.scenarios.default]\nrenderer = 'apps.fake:render_alt'\n"
    )
    with pytest.raises(ValueError, match="collides with the top-level renderer"):
        load_declarations(tmp_path)

    (tmp_path / "apps.toml").write_text(
        "[fake.viz.scenarios.'Bad Name']\nrenderer = 'apps.fake:render_alt'\n"
    )
    with pytest.raises(ValueError, match="scenario names"):
        load_declarations(tmp_path)

    (tmp_path / "apps.toml").write_text(
        "[fake.viz]\ndisplays = ['front']\n"
        "[fake.viz.scenarios.alt]\nrenderer = 'apps.fake:render_alt'\n"
    )
    with pytest.raises(ValueError, match="may only contain scenarios"):
        load_declarations(tmp_path)


def test_listing_scenarios_never_imports_the_app(checkout):
    DeclaredAdapter().scenarios()
    assert "apps.fake" not in sys.modules


def test_declared_render_carries_region_checks_and_provenance(checkout):
    segment = DeclaredAdapter().render(RenderRequest.from_values("fake/default"))

    track = segment.displays[0]
    assert (track.id, track.size, track.fps) == ("front", (72, 16), 1)
    assert track.confidence is Confidence.SOURCE_EXACT
    assert segment.evidence_level is EvidenceLevel.RENDERER_VERIFIED
    assert {check.id for check in segment.checks} == {
        "front-dimensions", "front-metrics", "front-loop-seam",
        "label-body", "label-contrast",
    }
    contrast_check = next(
        check for check in segment.checks if check.id == "label-contrast"
    )
    assert contrast_check.parameters["contrast_mode"] == "luminance_or_channel"
    assert any("apps.toml" in note for note in segment.notes)
    assert "apps/fake.py" in segment.source_paths

    results = {result.id: result for result in analyze(segment)}
    assert results["label-contrast"].status.value == "fail"
    assert results["label-contrast"].observed["worst_delta"] < 76.5
    assert not required_checks_pass(tuple(results.values()))


def test_declared_scenarios_accept_no_controls_or_inputs(checkout):
    request = RenderRequest.from_values("fake/default", {"hour": 3})
    with pytest.raises(ValueError, match="no controls or inputs"):
        DeclaredAdapter().render(request)
    with pytest.raises(KeyError, match="unknown declared scenario"):
        DeclaredAdapter().render(RenderRequest.from_values("missing/default"))


def test_missing_manifest_or_viz_blocks_mean_no_declared_scenarios(tmp_path):
    assert load_declarations(tmp_path) == ()
    (tmp_path / "apps.toml").write_text("[plain]\nkind = 'foreground'\n")
    assert load_declarations(tmp_path) == ()


@pytest.mark.parametrize(
    ("viz_body", "message"),
    [
        ("renderer = 'busybar_viz.declared:main'", "inside the apps package"),
        ("renderer = 'os:system'", "inside the apps package"),
        ("renderer = 'apps:render'", "inside the apps package"),
        ("renderer = 'apps.fake.render'", "inside the apps package"),
        ("", "renderer is required"),
        ("renderer = 'apps.fake:render_visual'\nsurprise = 1", "unknown keys"),
        (
            "renderer = 'apps.fake:render_visual'\ndisplays = ['front', 'front']",
            "unique subset",
        ),
        (
            "renderer = 'apps.fake:render_visual'\ndisplays = ['top']",
            "unique subset",
        ),
    ],
)
def test_declarations_fail_closed(tmp_path, viz_body, message):
    (tmp_path / "apps.toml").write_text(f"[fake.viz]\n{viz_body}\n")
    with pytest.raises(ValueError, match=message):
        load_declarations(tmp_path)


@pytest.mark.parametrize(
    ("region_body", "message"),
    [
        ("rect = [0, 0, 80, 10]", "inside the front"),
        ("rect = [5, 5, 5, 10]", "inside the front"),
        ("rect = [0, 0, 20]", "rect must be"),
        ("rect = [0, 0, 20, 10]\nink = ['#FFF']", "RRGGBB"),
        ("rect = [0, 0, 20, 10]\nmax_isolated = -1", "max_isolated"),
        (
            "rect = [0, 0, 20, 10]\ncontrast_mode = 'anything'",
            "contrast_mode",
        ),
        (
            "rect = [0, 0, 20, 10]\n"
            "contrast_mode = 'luminance_or_channel'",
            "requires at least one declared ink",
        ),
        ("rect = [0, 0, 20, 10]\ndisplay = 'back'", "undeclared display"),
        ("rect = [0, 0, 20, 10]\nglyph = 1", "unknown keys"),
    ],
)
def test_region_declarations_fail_closed(tmp_path, region_body, message):
    (tmp_path / "apps.toml").write_text(
        "[fake.viz]\nrenderer = 'apps.fake:render_visual'\n"
        f"[fake.viz.regions.label]\n{region_body}\n"
    )
    with pytest.raises(ValueError, match=message):
        load_declarations(tmp_path)


def test_renderer_contract_violations_are_named(checkout):
    bad = checkout / "apps" / "fake.py"
    bad.write_text(textwrap.dedent(
        '''
        from PIL import Image


        def render_visual():
            return {"back": ([Image.new("RGB", (160, 80))], 1)}
        '''
    ))
    sys.modules.pop("apps.fake", None)
    with pytest.raises(ValueError, match="expected display tracks"):
        DeclaredAdapter().render(RenderRequest.from_values("fake/default"))
