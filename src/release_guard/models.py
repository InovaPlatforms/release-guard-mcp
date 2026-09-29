"""Typed tool outputs. Each model becomes the tool's MCP outputSchema.

Field descriptions are written for the agent: they say what a value means and
what to do with it, because the model reads them when it plans the next step.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

CheckStatus = Literal["pass", "fail", "warn", "skip"]
VersionPhase = Literal["draft", "waiting_for_review", "in_review", "rejected", "approved",
                       "processing", "live", "replaced", "other"]


class BuildInfo(BaseModel):
    build_id: str = Field(description="App Store Connect build resource id (masked in redacted mode).")
    build_number: str = Field(description="CFBundleVersion, e.g. '3'.")
    processing_state: str = Field(description="PROCESSING, FAILED, INVALID or VALID. Only VALID builds can be attached.")
    uploaded_date: str | None = None
    expired: bool | None = Field(None, description="Expired builds cannot be submitted.")
    uses_non_exempt_encryption: bool | None = Field(
        None, description="Export compliance answer. null means not answered yet (blocks submission).")
    min_os_version: str | None = None


class BuildStatusReport(BaseModel):
    app_id: str
    version: str = Field(description="Marketing version (CFBundleShortVersionString) that was checked.")
    requested_build_number: str | None = Field(None, description="Build number asked about, or null for the latest.")
    verdict: Literal["VALID", "PROCESSING", "FAILED", "INVALID", "EXPIRED", "NOT_FOUND"] = Field(
        description="State of the selected build. VALID means it can be attached to the version.")
    ready_to_attach: bool
    build: BuildInfo | None = Field(None, description="The selected build (requested number, else newest upload).")
    other_builds: list[BuildInfo] = Field(default_factory=list, description="Other builds uploaded for this version.")
    next_step: str = Field(description="One-sentence recommendation for the user.")
    rate_limit_remaining: int | None = Field(None, description="Requests left this rolling hour for the API key.")


class VersionSummary(BaseModel):
    version_id: str
    version_string: str
    app_version_state: str | None = Field(None, description="Apple AppVersionState, e.g. PREPARE_FOR_SUBMISSION, WAITING_FOR_REVIEW.")
    app_store_state: str | None = Field(None, description="Legacy appStoreState, e.g. READY_FOR_SALE.")
    phase: VersionPhase = Field(description="Plain-language phase derived from the Apple state.")
    editable: bool = Field(description="True if metadata and build can still be changed.")
    release_type: str | None = None
    created_date: str | None = None
    attached_build_number: str | None = None
    attached_build_state: str | None = None


class ReviewSubmissionSummary(BaseModel):
    submission_id: str
    state: str = Field(description="READY_FOR_REVIEW (draft), WAITING_FOR_REVIEW, IN_REVIEW, UNRESOLVED_ISSUES, COMPLETE...")
    submitted_date: str | None = None


class VersionStateReport(BaseModel):
    app_id: str
    app_name: str | None = None
    requested_version: str | None = Field(None, description="Version asked about, or null for 'current'.")
    version: VersionSummary | None = Field(None, description="The requested version, else the newest one.")
    recent_versions: list[VersionSummary] = Field(default_factory=list)
    open_review_submissions: list[ReviewSubmissionSummary] = Field(default_factory=list)
    next_step: str


class LintFinding(BaseModel):
    rule_id: str
    severity: Literal["error", "warning"]
    message: str
    match: str = Field(description="The offending text.")
    start: int
    end: int
    guideline: str | None = Field(None, description="App Review Guideline the rule enforces.")


class LintReport(BaseModel):
    field: str
    locale: str | None = None
    char_count: int
    char_limit: int | None
    ok: bool = Field(description="False if any error-severity finding or the text is over the limit.")
    errors: int
    warnings: int
    findings: list[LintFinding] = Field(default_factory=list)


class CheckResult(BaseModel):
    id: str = Field(description="Stable check id, e.g. 'build_valid'.")
    title: str
    status: CheckStatus = Field(description="fail blocks submission; warn needs a human look; skip means not checkable.")
    detail: str = Field(description="What was found.")
    fix: str | None = Field(None, description="Concrete fix when status is fail or warn.")
    evidence: dict[str, Any] = Field(default_factory=dict)


class PreflightReport(BaseModel):
    app_id: str
    version: str
    build_number: str | None
    verdict: Literal["ready", "ready_with_warnings", "blocked"] = Field(
        description="blocked if any check failed; ready_with_warnings if any warned.")
    passed: int
    failed: int
    warned: int
    skipped: int
    blocking: list[str] = Field(default_factory=list, description="Ids of failed checks.")
    checks: list[CheckResult]
    mode: Literal["read_only"] = "read_only"
    generated_at: str


class DayCount(BaseModel):
    date: str
    delivered: int
    undelivered: int


class UndeliveredNotification(BaseModel):
    notification_uuid: str
    notification_type: str | None
    subtype: str | None = None
    signed_date: str | None
    attempts: int
    last_result: str | None = Field(None, description="Apple sendAttemptResult of the latest attempt.")


class ReconcileReport(BaseModel):
    environment: Literal["production", "sandbox"]
    window_days: int
    start: str
    end: str
    scope: Literal["all", "failures_only"] = Field(
        description="all: every notification in the window; failures_only: Apple's onlyFailures filter.")
    scanned: int
    delivered: int
    undelivered: int
    delivery_rate: float | None = Field(None, description="delivered / scanned, when scope is 'all'.")
    pages: int
    truncated: bool = Field(description="True if max_pages stopped the scan early.")
    by_day: list[DayCount]
    by_type: dict[str, int] = Field(description="Undelivered count per notificationType.")
    by_failure_reason: dict[str, int] = Field(description="Undelivered count per last sendAttemptResult.")
    samples: list[UndeliveredNotification] = Field(default_factory=list)
    replay_performed: Literal[False] = Field(False, description="Always false: this tool only reports.")
    next_step: str


class PlannedWrite(BaseModel):
    method: str
    path: str
    description: str


class SubmitReport(BaseModel):
    mode: Literal["dry_run", "refused", "submitted"] = Field(
        description="dry_run: nothing changed. refused: a gate said no. submitted: the version was sent to review.")
    version: str
    build_number: str
    preflight_verdict: str
    blocking_checks: list[str] = Field(default_factory=list)
    planned_writes: list[PlannedWrite]
    executed_writes: list[PlannedWrite] = Field(default_factory=list)
    refused_reason: str | None = None
    final_state: str | None = None
    next_step: str
