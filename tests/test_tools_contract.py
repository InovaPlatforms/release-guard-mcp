"""MCP contract tests: tool listing, schemas, annotations and every tool end-to-end
against the fake App Store Connect backend (recorded-shape responses, no network)."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from mcp import Client

from release_guard.fake_asc import FakeApple, Fault, demo_scenario
from release_guard.models import (
    BuildStatusReport,
    LintReport,
    PreflightReport,
    ReconcileReport,
    SubmitReport,
    VersionStateReport,
)
from release_guard.server import create_server

from .conftest import APP_ID, make_runtime, non_get_asc_requests

pytestmark = pytest.mark.anyio

TOOLS = ["check_build_status", "check_version_state", "preflight_submission", "reconcile_server_notifications",
         "lint_release_notes", "submit_for_review"]
MODELS = {"check_build_status": BuildStatusReport, "check_version_state": VersionStateReport,
          "preflight_submission": PreflightReport, "reconcile_server_notifications": ReconcileReport,
          "lint_release_notes": LintReport, "submit_for_review": SubmitReport}


async def call(client: Client, tool: str, args: dict) -> dict:
    result = await client.call_tool(tool, args)
    assert not result.is_error, result.content[0].text if result.content else result
    structured = result.structured_content
    assert isinstance(structured, dict)
    MODELS[tool].model_validate(structured)  # conforms to the advertised outputSchema
    assert json.loads(result.content[0].text) == structured  # text mirror for older clients
    return structured


def ready_scenario():
    s = demo_scenario()
    for b in s.builds:
        b["attributes"]["usesNonExemptEncryption"] = False
    v240 = s.versions[0]
    v240["relationships"]["build"]["data"] = {"type": "builds", "id": "bld-42-0000-gggg"}
    for loc in s.localizations["ver-240-0000-aaaa"]:
        loc["attributes"]["whatsNew"] = "Workout streaks and faster sync."
    for p in s.iaps + s.subscriptions["2100000001"]:
        p["attributes"]["state"] = "APPROVED"
    return s


# -- listing and schemas -----------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_lists_six_tools_for_handshake_and_modern_clients(runtime, mode: str) -> None:
    async with Client(create_server(runtime), mode=mode) as client:
        tools = (await client.list_tools()).tools
    assert [t.name for t in tools] == TOOLS  # deterministic order (spec SHOULD)


async def test_tool_metadata_is_complete_and_annotated(client: Client) -> None:
    tools = {t.name: t for t in (await client.list_tools()).tools}
    for name, tool in tools.items():
        assert re.fullmatch(r"[a-z_]{1,64}", name)
        assert tool.title and len(tool.description or "") >= 200, name
        assert tool.input_schema["type"] == "object" and tool.output_schema, name
        ann = tool.annotations
        assert ann is not None
        if name == "submit_for_review":
            assert ann.read_only_hint is False and ann.destructive_hint is True and ann.idempotent_hint is False
        else:
            assert ann.read_only_hint is True and ann.destructive_hint is False
    assert tools["lint_release_notes"].annotations.open_world_hint is False  # local only
    version = tools["check_build_status"].input_schema["properties"]["version"]
    assert version["pattern"] and "2.88" in version["description"]
    submit = tools["submit_for_review"].input_schema["properties"]
    assert submit["dry_run"]["default"] is True and submit["confirm"]["default"] is False


async def test_descriptions_point_to_the_right_sibling(client: Client) -> None:
    tools = {t.name: t.description for t in (await client.list_tools()).tools}
    assert "check_version_state" in tools["check_build_status"]
    assert "check_build_status" in tools["check_version_state"]
    assert "preflight_submission" in tools["lint_release_notes"]
    assert "Read-only" in tools["preflight_submission"] and "never submits" in tools["preflight_submission"]


async def test_invalid_arguments_return_a_tool_error(client: Client) -> None:
    result = await client.call_tool("check_build_status", {"version": "two point eight"})
    assert result.is_error
    result = await client.call_tool("reconcile_server_notifications", {"days": 500})
    assert result.is_error


# -- check_build_status ------------------------------------------------------------------------

async def test_build_processing_valid_and_missing(client: Client) -> None:
    out = await call(client, "check_build_status", {"version": "2.4.0", "build_number": "43"})
    assert out["verdict"] == "PROCESSING" and not out["ready_to_attach"]
    out = await call(client, "check_build_status", {"version": "2.4.0", "build_number": "42"})
    assert out["verdict"] == "VALID" and out["ready_to_attach"] and "Export compliance" in out["next_step"]
    out = await call(client, "check_build_status", {"version": "2.4.0"})
    assert out["build"]["build_number"] == "43"  # newest upload when no number is given
    out = await call(client, "check_build_status", {"version": "9.9"})
    assert out["verdict"] == "NOT_FOUND" and out["build"] is None
    assert out["rate_limit_remaining"] == 3500


async def test_expired_build_is_reported(client: Client) -> None:
    out = await call(client, "check_build_status", {"version": "2.3.0"})
    assert out["verdict"] == "EXPIRED"


# -- check_version_state -----------------------------------------------------------------------

async def test_version_state_newest_and_specific(client: Client) -> None:
    out = await call(client, "check_version_state", {})
    assert out["app_name"] == "Demo Fitness" and out["version"]["version_string"] == "2.4.0"
    assert out["version"]["phase"] == "draft" and out["version"]["editable"]
    out = await call(client, "check_version_state", {"version": "2.3.1"})
    assert out["version"]["phase"] == "live" and out["version"]["attached_build_number"] == "41"
    out = await call(client, "check_version_state", {"version": "7.0"})
    assert out["version"] is None and "No App Store version 7.0" in out["next_step"]


async def test_only_newest_live_version_counts_as_live() -> None:
    s = demo_scenario()
    s.versions[2]["attributes"].update(appVersionState="READY_FOR_DISTRIBUTION", appStoreState="READY_FOR_SALE")
    async with Client(create_server(make_runtime(FakeApple(s)))) as client:
        out = await call(client, "check_version_state", {})
    phases = {v["version_string"]: v["phase"] for v in out["recent_versions"]}
    assert phases == {"2.4.0": "draft", "2.3.1": "live", "2.3.0": "replaced"}


async def test_redacted_mode_masks_resource_ids(fake: FakeApple) -> None:
    async with Client(create_server(make_runtime(fake, redact_ids=True))) as client:
        out = await call(client, "check_version_state", {"version": "2.4.0"})
    assert out["version"]["version_id"] == "…aaaa"


# -- preflight_submission ----------------------------------------------------------------------

async def test_preflight_blocks_on_real_problems(client: Client, fake: FakeApple) -> None:
    out = await call(client, "preflight_submission", {"version": "2.4.0", "build_number": "42"})
    checks = {c["id"]: c for c in out["checks"]}
    assert out["verdict"] == "blocked"
    assert set(out["blocking"]) == {"export_compliance", "whats_new_present", "whats_new_policy"}
    assert "de-DE" in checks["whats_new_present"]["detail"]
    assert "pricing-claims" in checks["whats_new_policy"]["detail"]
    assert "external-purchase" in checks["whats_new_policy"]["detail"]
    assert checks["build_attached"]["status"] == "warn"
    assert checks["iap_ready_to_submit"]["status"] == "warn"
    assert set(checks["iap_ready_to_submit"]["evidence"]["ready_to_submit"]) == {
        "com.example.demofitness.coins.500", "com.example.demofitness.pro.yearly"}
    assert checks["review_notes_present"]["status"] == "pass"
    assert all(c["fix"] for c in out["checks"] if c["status"] in ("fail", "warn"))
    # The review notes body and demo account never appear in the output.
    blob = json.dumps(out)
    assert "reviewer@example.com" not in blob and "Profile > Streaks" not in blob
    assert non_get_asc_requests(fake) == []


async def test_preflight_ready_when_everything_is_in_place() -> None:
    fake = FakeApple(ready_scenario())
    async with Client(create_server(make_runtime(fake))) as client:
        out = await call(client, "preflight_submission", {"version": "2.4.0"})
    assert out["verdict"] == "ready", [c for c in out["checks"] if c["status"] != "pass"]
    assert out["build_number"] == "42"  # the attached build is used when none is given
    assert out["failed"] == 0 and out["warned"] == 0 and out["skipped"] == 2  # repo checks without a repo


async def test_preflight_fails_expected_iap_that_is_not_attached(client: Client) -> None:
    out = await call(client, "preflight_submission", {
        "version": "2.4.0", "build_number": "42",
        "expected_iap_product_ids": ["com.example.demofitness.pro.yearly", "com.example.missing"]})
    check = next(c for c in out["checks"] if c["id"] == "iap_ready_to_submit")
    assert check["status"] == "fail" and "com.example.missing" in check["detail"]


async def test_preflight_detects_other_version_in_review() -> None:
    s = ready_scenario()
    s.review_submissions.append({"type": "reviewSubmissions", "id": "sub-other",
                                 "attributes": {"state": "WAITING_FOR_REVIEW", "platform": "IOS"}})
    s.submission_items["sub-other"] = ["ver-231-0000-bbbb"]
    async with Client(create_server(make_runtime(FakeApple(s)))) as client:
        out = await call(client, "preflight_submission", {"version": "2.4.0"})
    assert out["blocking"] == ["no_conflicting_submission"]


async def test_preflight_version_in_review_is_not_editable() -> None:
    s = ready_scenario()
    s.versions[0]["attributes"].update(appVersionState="WAITING_FOR_REVIEW", appStoreState="WAITING_FOR_REVIEW")
    async with Client(create_server(make_runtime(FakeApple(s)))) as client:
        out = await call(client, "preflight_submission", {"version": "2.4.0"})
    check = next(c for c in out["checks"] if c["id"] == "version_editable")
    assert check["status"] == "fail" and "review pipeline" in check["fix"]


async def test_preflight_missing_version_and_first_release() -> None:
    s = ready_scenario()
    s.versions = [s.versions[0]]  # no live version: first release, What's New not required
    for loc in s.localizations["ver-240-0000-aaaa"]:
        loc["attributes"]["whatsNew"] = ""
    async with Client(create_server(make_runtime(FakeApple(s)))) as client:
        out = await call(client, "preflight_submission", {"version": "2.4.0"})
        checks = {c["id"]: c["status"] for c in out["checks"]}
        assert checks["whats_new_present"] == "skip"
        missing = await call(client, "preflight_submission", {"version": "3.0"})
    assert missing["blocking"][0] == "version_exists"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


async def test_preflight_repo_checks_build_number_and_commit(tmp_path: Path) -> None:
    repo = tmp_path / "app"
    (repo / "Config").mkdir(parents=True)
    (repo / "Config" / "AppVersion.xcconfig").write_text("MARKETING_VERSION = 2.4.0\nCURRENT_PROJECT_VERSION = 41\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "-c", "user.email=t@x", "-c", "user.name=t", "add", ".")
    _git(repo, "-c", "user.email=t@x", "-c", "user.name=t", "commit", "-qm", "init")
    _git(repo, "checkout", "-qb", "hotfix")
    (repo / "fix.txt").write_text("x")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.email=t@x", "-c", "user.name=t", "commit", "-qm", "hotfix")
    async with Client(create_server(make_runtime(FakeApple(ready_scenario())))) as client:
        out = await call(client, "preflight_submission", {"version": "2.4.0", "repo_path": str(repo)})
        checks = {c["id"]: c for c in out["checks"]}
        assert checks["build_number_monotonic"]["status"] == "fail"
        assert "44" in checks["build_number_monotonic"]["fix"]  # newest upload is 43
        assert checks["commit_on_main"]["status"] == "fail"  # HEAD is on hotfix, not main
        (repo / "Config" / "AppVersion.xcconfig").write_text("MARKETING_VERSION = 2.4.0\n"
                                                             "CURRENT_PROJECT_VERSION = 43\n")
        out = await call(client, "preflight_submission", {"version": "2.4.0", "repo_path": str(repo),
                                                          "commit": "main"})
        checks = {c["id"]: c for c in out["checks"]}
        assert checks["build_number_monotonic"]["status"] == "warn"  # submitting 42 while tree is at 43
        assert checks["commit_on_main"]["status"] == "pass"
        bad = await client.call_tool("preflight_submission", {"version": "2.4.0", "repo_path": str(repo),
                                                              "commit": "--upload-pack=x"})
        assert not bad.is_error  # reported as a failed check, never passed to git
        assert "not a valid git revision" in json.dumps(bad.structured_content)


async def test_preflight_recovers_from_a_rate_limit() -> None:
    s = ready_scenario()
    s.faults.append(Fault("/v1/builds", 429, count=1, headers={"Retry-After": "1"}))
    fake = FakeApple(s)
    async with Client(create_server(make_runtime(fake))) as client:
        out = await call(client, "preflight_submission", {"version": "2.4.0"})
    assert out["verdict"] == "ready"
    assert sum(1 for r in fake.requests if r.url.path == "/v1/builds") == 2


async def test_long_rate_limit_becomes_an_actionable_tool_error() -> None:
    s = demo_scenario()
    s.faults.append(Fault("/v1/builds", 429, count=5, headers={"Retry-After": "900"}))
    async with Client(create_server(make_runtime(FakeApple(s)))) as client:
        result = await client.call_tool("check_build_status", {"version": "2.4.0"})
    assert result.is_error
    text = result.content[0].text
    assert "rate limit" in text and "900s" in text and re.search(r"request rg-[0-9a-f]{10}", text)


async def test_permission_error_on_one_section_skips_that_check() -> None:
    s = ready_scenario()
    s.faults.append(Fault("/v1/apps/1234567890/inAppPurchasesV2", 403, count=1))
    async with Client(create_server(make_runtime(FakeApple(s)))) as client:
        out = await call(client, "preflight_submission", {"version": "2.4.0"})
    check = next(c for c in out["checks"] if c["id"] == "iap_ready_to_submit")
    assert check["status"] == "skip" and "403" in check["detail"]


# -- reconcile_server_notifications -------------------------------------------------------------

async def test_reconcile_failures_only(client: Client) -> None:
    out = await call(client, "reconcile_server_notifications", {"days": 14})
    assert (out["scanned"], out["delivered"], out["undelivered"]) == (4, 1, 3)
    assert out["by_failure_reason"] == {"UNSUCCESSFUL_HTTP_RESPONSE_CODE": 2, "NO_RESPONSE": 1}
    assert out["replay_performed"] is False and "only reports" in out["next_step"]
    assert out["delivery_rate"] is None


async def test_reconcile_all_paginates_and_computes_rate(client: Client) -> None:
    out = await call(client, "reconcile_server_notifications", {"days": 14, "scope": "all"})
    assert out["scanned"] == 40 and out["pages"] == 2 and not out["truncated"]
    assert out["delivery_rate"] == round(37 / 40, 4)
    out = await call(client, "reconcile_server_notifications", {"days": 14, "scope": "all", "max_pages": 1})
    assert out["truncated"] and "max_pages" in out["next_step"]


async def test_reconcile_window_limits(client: Client) -> None:
    result = await client.call_tool("reconcile_server_notifications", {"days": 60, "environment": "sandbox"})
    assert result.is_error and "30" in result.content[0].text


# -- lint_release_notes -------------------------------------------------------------------------

async def test_lint_over_mcp(client: Client) -> None:
    out = await call(client, "lint_release_notes", {"text": "Now 20% off! Also on Android.", "field": "whats_new"})
    assert not out["ok"] and {f["rule_id"] for f in out["findings"]} == {"pricing-claims", "other-platforms"}
    out = await call(client, "lint_release_notes", {"text": "Live Sports Intel", "field": "subtitle"})
    assert out["ok"] and out["char_limit"] == 30


# -- submit_for_review (never writes in tests) ---------------------------------------------------

async def test_submit_defaults_to_a_dry_run_plan(client: Client, fake: FakeApple) -> None:
    out = await call(client, "submit_for_review", {"version": "2.4.0", "build_number": "42"})
    assert out["mode"] == "dry_run" and out["executed_writes"] == []
    assert [w["method"] for w in out["planned_writes"]] == ["PATCH", "POST", "POST", "PATCH"]
    assert "Preflight is blocked" in out["next_step"]
    assert non_get_asc_requests(fake) == []


async def test_confirm_alone_is_still_a_dry_run(client: Client, fake: FakeApple) -> None:
    out = await call(client, "submit_for_review", {"version": "2.4.0", "build_number": "42", "confirm": True})
    assert out["mode"] == "dry_run"
    assert non_get_asc_requests(fake) == []


async def test_execute_without_confirm_is_refused(client: Client, fake: FakeApple) -> None:
    out = await call(client, "submit_for_review", {"version": "2.4.0", "build_number": "42", "dry_run": False})
    assert out["mode"] == "refused" and "confirm" in out["refused_reason"]
    assert non_get_asc_requests(fake) == []


async def test_read_only_server_refuses_even_with_confirm(client: Client, fake: FakeApple) -> None:
    out = await call(client, "submit_for_review", {"version": "2.4.0", "build_number": "42", "dry_run": False,
                                                   "confirm": True})
    assert out["mode"] == "refused" and "read-only" in out["refused_reason"]
    assert non_get_asc_requests(fake) == []


async def test_writes_never_run_under_tests_even_when_enabled() -> None:
    fake = FakeApple(ready_scenario())
    async with Client(create_server(make_runtime(fake, allow_writes=True))) as client:
        out = await call(client, "submit_for_review", {"version": "2.4.0", "build_number": "42",
                                                       "dry_run": False, "confirm": True})
    assert out["mode"] == "refused" and "test" in out["refused_reason"]
    assert non_get_asc_requests(fake) == []


# -- configuration errors and logging ----------------------------------------------------------

async def test_missing_credentials_name_the_env_vars() -> None:
    from release_guard.config import Settings
    from release_guard.policy import default_policy
    from release_guard.runtime import Runtime

    runtime = Runtime(settings=Settings(app_id=APP_ID), policy=default_policy())  # live, no creds
    async with Client(create_server(runtime)) as client:
        result = await client.call_tool("check_build_status", {"version": "2.4.0"})
    assert result.is_error
    text = result.content[0].text
    assert "ASC_KEY_ID" in text and "ASC_ISSUER_ID" in text and "ASC_PRIVATE_KEY_PATH" in text


async def test_missing_app_id_is_explained(fake: FakeApple) -> None:
    runtime = make_runtime(fake)
    object.__setattr__(runtime.settings, "app_id", None)
    async with Client(create_server(runtime)) as client:
        result = await client.call_tool("check_build_status", {"version": "2.4.0"})
    assert result.is_error and "ASC_APP_ID" in result.content[0].text


async def test_tool_logs_are_structured_and_secret_free(fake: FakeApple, key_file: Path,
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    runtime = make_runtime(fake, key_id="KEYID12345", issuer_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
                           private_key_path=key_file)
    fake.require_auth = True
    async with Client(create_server(runtime)) as client:
        await call(client, "check_build_status", {"version": "2.4.0"})
    assert all(r.headers["authorization"].startswith("Bearer ey") for r in fake.requests)
    err = capsys.readouterr().err
    events = [json.loads(line) for line in err.splitlines() if line.startswith("{")]
    names = [e["event"] for e in events]
    assert names[:1] == ["tool_call"] and "http_request" in names and names[-1] == "tool_result"
    rids = {e["request_id"] for e in events}
    assert len(rids) == 1 and re.fullmatch(r"rg-[0-9a-f]{10}", rids.pop())
    for secret in ("KEYID12345", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "eyJ", "PRIVATE KEY"):
        assert secret not in err
