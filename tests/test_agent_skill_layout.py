"""Project skills are shared across harnesses without copied instructions."""

from __future__ import annotations

import os
import re
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
CLAUDE_SKILLS = REPO / ".claude" / "skills"
CODEX_SKILLS = REPO / ".agents" / "skills"
SKILLS = ("busybar-app", "busybar-viz")


def _frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text()
    assert text.startswith("---\n"), f"{path} has no YAML frontmatter"
    header, marker, _body = text[4:].partition("\n---\n")
    assert marker, f"{path} has unterminated YAML frontmatter"

    values: dict[str, str] = {}
    for line in header.splitlines():
        key, separator, value = line.partition(":")
        assert separator and key and value.strip(), f"invalid frontmatter: {line!r}"
        assert key not in values, f"duplicate frontmatter key: {key}"
        values[key] = value.strip()
    return values


def _quoted_yaml_value(text: str, key: str) -> str:
    match = re.search(rf'^  {re.escape(key)}: "([^"]*)"$', text, re.MULTILINE)
    assert match, f"agents/openai.yaml has no quoted {key}"
    return match.group(1)


def test_codex_skills_are_relative_links_to_the_one_authored_copy():
    for name in SKILLS:
        authored = CLAUDE_SKILLS / name
        discovery = CODEX_SKILLS / name

        assert authored.is_dir()
        assert not authored.is_symlink(), f"{authored} must be the authored copy"
        assert discovery.is_symlink(), f"{discovery} must be a discovery link"
        assert os.readlink(discovery) == f"../../.claude/skills/{name}"
        assert discovery.resolve(strict=True) == authored.resolve(strict=True)
        assert (discovery / "SKILL.md").samefile(authored / "SKILL.md")


def test_skill_frontmatter_is_portable_and_complete():
    for name in SKILLS:
        skill = CLAUDE_SKILLS / name / "SKILL.md"
        values = _frontmatter(skill)
        assert values.keys() == {"name", "description"}
        assert values["name"] == name
        assert len(values["description"]) >= 40
        assert "TODO" not in skill.read_text()


def test_busybar_viz_has_generated_openai_interface_metadata():
    metadata = (CLAUDE_SKILLS / "busybar-viz" / "agents" / "openai.yaml")
    text = metadata.read_text()

    assert _quoted_yaml_value(text, "display_name") == "BUSY Bar Visualizer"
    short = _quoted_yaml_value(text, "short_description")
    assert 25 <= len(short) <= 64
    prompt = _quoted_yaml_value(text, "default_prompt")
    assert "$busybar-viz" in prompt


def test_root_guidance_routes_both_skills_and_preserves_boundaries():
    agents = (REPO / "AGENTS.md").read_text()
    claude = (REPO / "CLAUDE.md").read_text()
    app_skill = (CLAUDE_SKILLS / "busybar-app" / "SKILL.md").read_text()

    for guidance in (agents, claude):
        assert "busybar-app" in guidance
        assert "busybar-viz" in guidance
        assert "Barkeep" in guidance
        assert "hardware" in guidance.lower()

    assert "Read `AGENTS.md` first" in app_skill
    assert "Read `CLAUDE.md` first" not in app_skill


def test_local_links_in_changed_guidance_resolve():
    sources = (
        REPO / "AGENTS.md",
        REPO / "CLAUDE.md",
        REPO / "docs" / "README.md",
        REPO / "docs" / "busybar-viz.md",
        REPO / "docs" / "design" / "README.md",
        CLAUDE_SKILLS / "busybar-viz" / "SKILL.md",
    )
    link = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    missing: list[str] = []

    for source in sources:
        for target in link.findall(source.read_text()):
            if target.startswith(("https://", "http://", "#")):
                continue
            local = target.split("#", 1)[0]
            if local and not (source.parent / local).exists():
                missing.append(f"{source.relative_to(REPO)} -> {target}")

    assert not missing, "broken local guidance links:\n  " + "\n  ".join(missing)


# --- the skill must not drift from the code --------------------------------
#
# A guide that is confidently wrong is worse than none. All three of these
# were found stale: the line count understated skystrip by half, the siren
# timings were 3x off, and the mistakes table prescribed a font the same
# file's law #6 says is broken.


def _skill() -> str:
    return (Path(__file__).resolve().parents[1]
            / ".claude" / "skills" / "busybar-app" / "SKILL.md").read_text()


def test_the_skill_quotes_no_stale_line_count():
    """It said 3,700; skystrip is roughly double that. A hardcoded line count
    drifts by construction, so the fix was to stop quoting one."""
    import re
    assert not re.search(r"\d,\d00 lines", _skill()), (
        "the skill quotes a line count, which will drift again")


def test_the_siren_timings_match_the_code():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))
    import skystrip

    skill = _skill()
    assert f"every {skystrip.SIREN_RETRIGGER_S:.0f}s" in skill, (
        f"the skill's retrigger interval is not SIREN_RETRIGGER_S "
        f"({skystrip.SIREN_RETRIGGER_S})")
    assert f"for a {skystrip.SIREN_SECONDS}s" in skill, (
        f"the skill's clip length is not SIREN_SECONDS "
        f"({skystrip.SIREN_SECONDS})")


def test_the_mistakes_table_does_not_contradict_the_font_law():
    """The table prescribed '4x5' while law #6 in the same file explains that
    four columns turns M and W into filled rectangles. Someone reading only
    the table would build the font the body says is broken."""
    skill = _skill()
    assert "4x5, slashed zero" not in skill
    assert "5 columns for M/W" in skill or "five columns" in skill
