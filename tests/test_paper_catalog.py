"""Paper identifiers are the public identity of compositional generation."""

import pytest
import yaml

import main


def test_paper_rule_resolves_shared_anchor_without_relabeling_optional():
    from paper_patterns import compositional_rule

    rule = compositional_rule("P10")
    assert (rule.paper_pattern, rule.legacy_pattern, rule.form) == ("P10", "P11", "shared_anchor")
    optional = compositional_rule("P11")
    assert (optional.legacy_pattern, optional.form) == ("P13", "optional_branches")
    assert compositional_rule("P10", "independent").legacy_pattern == "P10"
    assert compositional_rule("P10", "chain").legacy_pattern == "P12"


def test_compositional_config_defaults_to_the_paper_catalog(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "database": {"dedicated": True, "allow_reset": True},
        "generation": {"composition": {"contexts": ["count", "with"]}},
    }))
    config = main.read_config(path)
    assert config["pattern_catalog"] == "paper-v1"
    assert config["patterns"] == [*(f"P{i}" for i in range(1, 14)), *(f"S{i}" for i in range(1, 11))]


def test_compositional_generation_cannot_implicitly_use_legacy_identifiers(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "database": {"dedicated": True, "allow_reset": True},
        "patterns": ["P3"], "pattern_catalog": "legacy-v1",
        "generation": {"composition": {"contexts": []}},
    }))
    with pytest.raises(ValueError, match="paper-v1"):
        main.read_config(path)


@pytest.mark.parametrize("pattern", ["P3", "P4", "P10"])
def test_resolved_composition_options_can_regenerate_the_same_input(pattern):
    first = main.prepare_case(None, pattern, 7, 2, 9, composition={"contexts": ["count", "with"]})
    second = main.prepare_case(None, pattern, 7, 2, 9, composition=first["composition"])
    assert second == first
