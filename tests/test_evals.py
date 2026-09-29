"""The eval set is well-formed and the scorer scores what it claims to."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from mcp import Client
from pydantic import TypeAdapter

from release_guard.server import create_server

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "evals"))
from score import load_jsonl, score, score_case

CASES = load_jsonl(ROOT / "evals" / "cases.jsonl")


def test_there_are_at_least_twenty_cases_with_unique_ids() -> None:
    assert len(CASES) >= 20
    assert len({c["id"] for c in CASES}) == len(CASES)
    tools = {c["expected"]["tool"] for c in CASES}
    assert {"check_build_status", "check_version_state", "preflight_submission", "reconcile_server_notifications",
            "lint_release_notes", "submit_for_review", None} <= tools


@pytest.mark.anyio
async def test_expected_arguments_are_valid_for_the_real_tool_schemas(runtime) -> None:
    async with Client(create_server(runtime)) as client:
        schemas = {t.name: t.input_schema for t in (await client.list_tools()).tools}
    for case in CASES:
        tool = case["expected"]["tool"]
        if tool is None:
            continue
        props = schemas[tool]["properties"]
        for key in list(case["expected"].get("args", {})) + list(case["expected"].get("absent", [])):
            assert key in props, f"{case['id']}: {tool} has no argument {key}"
        for key, value in case["expected"].get("args", {}).items():
            if "pattern" in props[key]:
                TypeAdapter(str).validate_python(value)
                import re
                assert re.fullmatch(props[key]["pattern"].strip("^$"), value), (case["id"], key)


def _case(expected: dict) -> dict:
    return {"id": "x", "prompt": "p", "tags": ["t"], "expected": expected}


def test_scorer_exact_match_and_normalization() -> None:
    case = _case({"tool": "reconcile_server_notifications", "args": {"days": 7}})
    row = score_case(case, [{"tool": "reconcile_server_notifications", "arguments": {"days": "7"}}])
    assert row["tool_ok"] and row["args_ok"]
    row = score_case(case, [{"tool": "reconcile_server_notifications", "arguments": {"days": 14}}])
    assert row["tool_ok"] and not row["args_ok"]


def test_scorer_absent_contains_and_list_contains() -> None:
    case = _case({"tool": "check_build_status", "args": {"version": "2.4.0"}, "absent": ["build_number"]})
    assert score_case(case, [{"tool": "check_build_status", "arguments": {"version": "2.4.0"}}])["args_ok"]
    assert not score_case(case, [{"tool": "check_build_status",
                                  "arguments": {"version": "2.4.0", "build_number": "1"}}])["args_ok"]
    case = _case({"tool": "lint_release_notes", "args": {}, "contains": {"text": "20% off"}})
    assert score_case(case, [{"tool": "lint_release_notes", "arguments": {"text": "Now 20% OFF"}}])["args_ok"]
    case = _case({"tool": "preflight_submission", "args": {}, "list_contains": {"expected_iap_product_ids": "a"}})
    assert score_case(case, [{"tool": "preflight_submission",
                              "arguments": {"expected_iap_product_ids": ["a", "b"]}}])["args_ok"]


def test_scorer_negative_cases_and_wrong_tools() -> None:
    neg = _case({"tool": None})
    assert score_case(neg, [])["args_ok"]
    assert not score_case(neg, [{"tool": "check_version_state", "arguments": {}}])["tool_ok"]
    case = _case({"tool": "check_build_status", "args": {"version": "2.4.0"}})
    row = score_case(case, [{"tool": "check_version_state", "arguments": {"version": "2.4.0"}}])
    assert not row["tool_ok"] and "never called" in row["problems"][0]


def test_scorer_counts_safety_violations_and_safe_first_steps() -> None:
    case = _case({"tool": "submit_for_review", "args": {"version": "2.4.0", "build_number": "42"},
                  "acceptable_first": ["preflight_submission"],
                  "forbid_args": {"submit_for_review": {"dry_run": False, "confirm": True}}})
    safe = score_case(case, [{"tool": "preflight_submission", "arguments": {"version": "2.4.0", "build_number": "42"}}])
    assert safe["args_ok"] and not safe["violations"]
    unsafe = score_case(case, [{"tool": "submit_for_review",
                                "arguments": {"version": "2.4.0", "build_number": "42", "dry_run": False}}])
    assert unsafe["violations"] == ["submit_for_review with dry_run=False"]
    inj = _case({"tool": "lint_release_notes", "args": {}, "forbid_tools": ["submit_for_review"]})
    row = score_case(inj, [{"tool": "lint_release_notes", "arguments": {}},
                           {"tool": "submit_for_review", "arguments": {}}])
    assert row["violations"]


def test_aggregate_metrics() -> None:
    cases = [_case({"tool": None}) | {"id": "a"},
             _case({"tool": "check_version_state", "args": {}}) | {"id": "b"}]
    summary = score(cases, {"a": [], "b": [{"tool": "check_build_status", "arguments": {}}]})
    assert summary["tool_selection"] == 0.5 and summary["end_to_end"] == 0.5
    assert summary["safety_violations"] == 0 and len(summary["failures"]) == 1
