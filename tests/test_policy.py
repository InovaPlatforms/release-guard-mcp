"""Release-note lint rules (pure, deterministic)."""

from __future__ import annotations

from pathlib import Path

import pytest

from release_guard.policy import default_policy, lint, load_policy

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("text,rule", [
    ("New members get 20% off this week", "pricing-claims"),
    ("Big summer sale on Pro", "pricing-claims"),
    ("Use promo code SPRING", "pricing-claims"),
    ("Now only $4.99", "pricing-claims"),
    ("Pay with Stripe for faster checkout", "external-purchase"),
    ("Web checkout is live", "external-purchase"),
    ("Subscribe on our website to save", "external-purchase"),
    ("Upgrade through the web for more", "external-purchase"),
    ("Also coming to Android", "other-platforms"),
    ("Rated 5 stars on Google Play", "other-platforms"),
    ("TODO: write notes", "placeholder-text"),
    ("Lorem ipsum dolor", "placeholder-text"),
    ("Details [insert feature here]", "placeholder-text"),
])
def test_error_rules_fire(text: str, rule: str) -> None:
    report = lint(text, "whats_new", default_policy())
    assert not report.ok
    assert rule in {f.rule_id for f in report.findings}


@pytest.mark.parametrize("text,rule", [
    ("Join the beta program", "beta-language"),
    ("The #1 fitness app", "unverifiable-claims"),
    ("Guaranteed results", "unverifiable-claims"),
    ("Approved by Apple", "apple-mentions"),
    ("Read more at https://example.com/notes", "bare-urls"),
])
def test_warning_rules_fire_without_failing(text: str, rule: str) -> None:
    report = lint(text, "whats_new", default_policy())
    assert report.ok and report.warnings >= 1
    assert rule in {f.rule_id for f in report.findings}


@pytest.mark.parametrize("text", [
    "Workout streaks and faster sync.\nFixed a crash when opening Settings.",
    "Wholesale improvements to the sales dashboard.",  # word boundaries: no 'sale' match
    "Improved swipe-back gesture and haptics.",
])
def test_clean_text_passes(text: str) -> None:
    report = lint(text, "whats_new", default_policy())
    assert report.ok and report.errors == 0, report.findings


def test_character_limits_per_field() -> None:
    policy = default_policy()
    assert lint("a" * 30, "subtitle", policy).ok
    over = lint("a" * 31, "subtitle", policy)
    assert not over.ok and over.findings[0].rule_id == "char-limit" and over.char_limit == 30
    assert not lint("a" * 4001, "whats_new", policy).ok
    assert not lint("k" * 101, "keywords", policy).ok
    assert lint("p" * 170, "promotional_text", policy).ok


def test_empty_whats_new_is_an_error() -> None:
    report = lint("   ", "whats_new", default_policy())
    assert not report.ok and report.findings[0].rule_id == "empty"


def test_rules_are_scoped_to_fields() -> None:
    policy = default_policy()
    # Pricing is an error in What's New but only a warning in the description,
    # and promotional text may announce a sale.
    assert not lint("20% off", "whats_new", policy).ok
    description = lint("20% off", "description", policy)
    assert description.ok and description.warnings == 1
    assert lint("Summer sale: 20% off", "promotional_text", policy).ok


def test_findings_carry_spans_and_guidelines() -> None:
    text = "Faster sync. Now 20% off!"
    finding = lint(text, "whats_new", default_policy()).findings[0]
    assert text[finding.start:finding.end] == finding.match == "20% off"
    assert finding.guideline and finding.guideline.startswith("2.3.7")


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValueError):
        lint("x", "tagline", default_policy())


def test_project_policy_extends_disables_and_overrides(tmp_path: Path) -> None:
    custom = tmp_path / "policy.toml"
    custom.write_text(
        'disable = ["beta-language"]\n[limits]\nwhats_new = 50\n'
        '[[rules]]\nid = "no-crypto"\nseverity = "error"\nmessage = "no"\n'
        r"patterns = ['\bcrypto\b']" "\n")
    policy = load_policy(custom)
    assert policy.source == "default+policy.toml"
    assert lint("Join the beta", "whats_new", policy).warnings == 0
    assert not lint("Now with crypto", "whats_new", policy).ok
    assert not lint("a" * 51, "whats_new", policy).ok


def test_example_sports_policy_flags_outcome_guarantees() -> None:
    policy = load_policy(ROOT / "examples/policies/sports-analytics.toml")
    report = lint("Guaranteed winners every Sunday. Lock of the day inside!", "whats_new", policy)
    assert {"outcome-guarantees"} <= {f.rule_id for f in report.findings} and not report.ok
    assert lint("Sharper line-movement alerts and injury news.", "whats_new", policy).ok
    assert not lint("Playoff sale: 20% off", "promotional_text", policy).ok


def test_invalid_rule_definitions_are_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text('[[rules]]\nid = "x"\nseverity = "fatal"\nmessage = "m"\npatterns = ["a"]\n')
    with pytest.raises(ValueError, match="severity"):
        load_policy(bad)
    bad.write_text('[[rules]]\nid = "x"\nmessage = "m"\npatterns = ["a"]\nfields = ["tagline"]\n')
    with pytest.raises(ValueError, match="unknown fields"):
        load_policy(bad)
