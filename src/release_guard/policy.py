"""Release-note and metadata lint rules.

Pure functions with no network access, so `lint_release_notes` is safe to call
on any draft text and is fully deterministic in tests.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Literal

from .models import LintFinding, LintReport

Field = Literal["name", "subtitle", "promotional_text", "keywords", "description", "whats_new", "review_notes"]
FIELDS: tuple[str, ...] = ("name", "subtitle", "promotional_text", "keywords", "description", "whats_new",
                           "review_notes")


@dataclass(frozen=True)
class Rule:
    id: str
    severity: Literal["error", "warning"]
    message: str
    patterns: tuple[re.Pattern[str], ...]
    guideline: str | None = None
    fields: frozenset[str] = frozenset()  # empty = every field

    def applies_to(self, field_name: str) -> bool:
        return not self.fields or field_name in self.fields


@dataclass(frozen=True)
class Policy:
    rules: tuple[Rule, ...]
    limits: dict[str, int] = field(default_factory=dict)
    source: str = "default"


def _rule(raw: dict) -> Rule:
    severity = raw.get("severity", "error")
    if severity not in ("error", "warning"):
        raise ValueError(f"rule {raw.get('id')}: severity must be error or warning")
    fields = frozenset(raw.get("fields") or ())
    unknown = fields - set(FIELDS)
    if unknown:
        raise ValueError(f"rule {raw.get('id')}: unknown fields {sorted(unknown)}")
    return Rule(
        id=str(raw["id"]),
        severity=severity,
        message=str(raw["message"]),
        patterns=tuple(re.compile(p, re.IGNORECASE) for p in raw["patterns"]),
        guideline=raw.get("guideline"),
        fields=fields,
    )


def _parse(data: dict, base: Policy | None, source: str) -> Policy:
    disabled = set(data.get("disable") or ())
    rules = [r for r in (base.rules if base else ()) if r.id not in disabled]
    new_rules = [_rule(r) for r in data.get("rules") or ()]
    replaced = {r.id for r in new_rules}
    rules = [r for r in rules if r.id not in replaced] + new_rules
    limits = dict(base.limits if base else {})
    limits.update({k: int(v) for k, v in (data.get("limits") or {}).items()})
    return Policy(rules=tuple(rules), limits=limits, source=source)


def default_policy() -> Policy:
    text = resources.files("release_guard").joinpath("policy_default.toml").read_text()
    return _parse(tomllib.loads(text), None, "default")


def load_policy(path: Path | None = None) -> Policy:
    """Default policy, optionally extended by a project TOML file."""
    base = default_policy()
    if not path:
        return base
    data = tomllib.loads(Path(path).read_text())
    return _parse(data, base, f"default+{Path(path).name}")


def lint(text: str, field_name: str, policy: Policy, locale: str | None = None) -> LintReport:
    if field_name not in FIELDS:
        raise ValueError(f"field must be one of {', '.join(FIELDS)}")
    findings: list[LintFinding] = []
    seen: set[tuple[str, int, int]] = set()
    for rule in policy.rules:
        if not rule.applies_to(field_name):
            continue
        for pattern in rule.patterns:
            for match in pattern.finditer(text):
                key = (rule.id, match.start(), match.end())
                if key in seen:
                    continue
                seen.add(key)
                findings.append(LintFinding(rule_id=rule.id, severity=rule.severity, message=rule.message,
                                            match=match.group(0), start=match.start(), end=match.end(),
                                            guideline=rule.guideline))
    limit = policy.limits.get(field_name)
    count = len(text)
    if limit is not None and count > limit:
        findings.append(LintFinding(rule_id="char-limit", severity="error",
                                    message=f"{count} characters; App Store Connect allows {limit}.",
                                    match=text[limit:limit + 20], start=limit, end=count,
                                    guideline="App Store Connect field limit"))
    if field_name == "whats_new" and not text.strip():
        findings.append(LintFinding(rule_id="empty", severity="error",
                                    message="What's New is empty; App Store Connect requires it for updates.",
                                    match="", start=0, end=0, guideline=None))
    findings.sort(key=lambda f: (f.severity != "error", f.start))
    errors = sum(1 for f in findings if f.severity == "error")
    return LintReport(field=field_name, locale=locale, char_count=count, char_limit=limit, ok=errors == 0,
                      errors=errors, warnings=len(findings) - errors, findings=findings)
