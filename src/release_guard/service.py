"""Tool logic, independent of MCP so it can be unit-tested directly."""

from __future__ import annotations

import collections
import datetime as dt
from pathlib import Path
from typing import Any

from . import repo as gitrepo
from .asc import (
    EDITABLE_STATES,
    IAP_ATTACHED_STATES,
    IAP_BLOCKING_STATES,
    LIVE_STATES,
    OPEN_SUBMISSION_STATES,
    is_editable,
    phase_of,
    version_state,
)
from .auth import running_under_test
from .logs import log
from .models import (
    BuildInfo,
    BuildStatusReport,
    CheckResult,
    DayCount,
    LintReport,
    PlannedWrite,
    PreflightReport,
    ReconcileReport,
    ReviewSubmissionSummary,
    SubmitReport,
    UndeliveredNotification,
    VersionStateReport,
    VersionSummary,
)
from .policy import lint
from .runtime import Runtime
from .server_api import decode_signed_payload, delivered
from .transport import ApiError, issue_permit


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class ReleaseGuard:
    def __init__(self, runtime: Runtime) -> None:
        self.rt = runtime

    # -- helpers -----------------------------------------------------------------------------
    def app_id(self, app_id: str | None) -> str:
        value = app_id or self.rt.settings.app_id
        if not value:
            raise ValueError("app_id is required (or set ASC_APP_ID in the server environment)")
        return value

    def _build(self, raw: dict[str, Any]) -> BuildInfo:
        a = raw.get("attributes") or {}
        return BuildInfo(build_id=self.rt.mask(raw["id"]) or "", build_number=str(a.get("version") or "?"),
                         processing_state=a.get("processingState") or "UNKNOWN",
                         uploaded_date=a.get("uploadedDate"), expired=a.get("expired"),
                         uses_non_exempt_encryption=a.get("usesNonExemptEncryption"),
                         min_os_version=a.get("minOsVersion"))

    def _version(self, raw: dict[str, Any], builds: dict[str, dict[str, Any]]) -> VersionSummary:
        a = raw.get("attributes") or {}
        ref = ((raw.get("relationships") or {}).get("build") or {}).get("data") or {}
        build = builds.get(ref.get("id") or "")
        ba = (build or {}).get("attributes") or {}
        return VersionSummary(version_id=self.rt.mask(raw["id"]) or "", version_string=a.get("versionString") or "?",
                              app_version_state=a.get("appVersionState"), app_store_state=a.get("appStoreState"),
                              phase=phase_of(a), editable=is_editable(a), release_type=a.get("releaseType"),
                              created_date=a.get("createdDate"),
                              attached_build_number=ba.get("version") if build else None,
                              attached_build_state=ba.get("processingState") if build else None)

    # -- check_build_status ------------------------------------------------------------------
    async def check_build_status(self, version: str, build_number: str | None = None,
                                 app_id: str | None = None) -> BuildStatusReport:
        app = self.app_id(app_id)
        asc = self.rt.asc()
        raw = await asc.builds(app, version, limit=20)
        builds = [self._build(b) for b in raw]
        if build_number:
            selected = next((b for b in builds if b.build_number == build_number), None)
        else:
            selected = builds[0] if builds else None
        others = [b for b in builds if b is not selected]
        if selected is None:
            what = f"build {build_number} of {version}" if build_number else f"any build of {version}"
            return BuildStatusReport(
                app_id=app, version=version, requested_build_number=build_number, verdict="NOT_FOUND",
                ready_to_attach=False, build=None, other_builds=others,
                next_step=(f"App Store Connect has no {what}. If you just uploaded it, wait a few minutes; "
                           "otherwise upload it with Xcode or Transporter."),
                rate_limit_remaining=asc.client.rate_limit.remaining)
        verdict = "EXPIRED" if selected.expired else selected.processing_state
        if verdict not in ("VALID", "PROCESSING", "FAILED", "INVALID", "EXPIRED"):
            verdict = "PROCESSING"
        steps = {
            "VALID": f"Build {selected.build_number} is VALID and can be attached to {version}.",
            "PROCESSING": "Apple is still processing this build; check again in a few minutes.",
            "FAILED": "Processing failed. Read the App Store Connect email, fix, bump the build number, re-upload.",
            "INVALID": "Apple marked the binary invalid. Read the App Store Connect email, fix, bump the build "
                       "number and re-upload.",
            "EXPIRED": "This build expired and cannot be submitted. Upload a new build.",
        }
        next_step = steps[verdict]
        if verdict == "VALID" and selected.uses_non_exempt_encryption is None:
            next_step += " Export compliance is unanswered on this build."
        return BuildStatusReport(app_id=app, version=version, requested_build_number=build_number,
                                 verdict=verdict, ready_to_attach=verdict == "VALID", build=selected,
                                 other_builds=others, next_step=next_step,
                                 rate_limit_remaining=asc.client.rate_limit.remaining)

    # -- check_version_state -----------------------------------------------------------------
    async def check_version_state(self, version: str | None = None,
                                  app_id: str | None = None) -> VersionStateReport:
        app = self.app_id(app_id)
        asc = self.rt.asc()
        app_raw = await asc.app(app)
        raw_versions, builds = await asc.versions(app, limit=8)
        target_raw = None
        if version:
            target_raw = next((v for v in raw_versions if (v.get("attributes") or {}).get("versionString") == version),
                              None)
            if target_raw is None:
                filtered, more_builds = await asc.versions(app, version=version, limit=1)
                builds.update(more_builds)
                target_raw = filtered[0] if filtered else None
        elif raw_versions:
            target_raw = raw_versions[0]
        subs = await asc.review_submissions(app, limit=10)
        open_subs = [ReviewSubmissionSummary(submission_id=self.rt.mask(s["id"]) or "",
                                             state=(s.get("attributes") or {}).get("state") or "?",
                                             submitted_date=(s.get("attributes") or {}).get("submittedDate"))
                     for s in subs if (s.get("attributes") or {}).get("state") in OPEN_SUBMISSION_STATES]
        target = self._version(target_raw, builds) if target_raw else None
        if target is None:
            next_step = (f"No App Store version {version} exists yet. Create it in App Store Connect."
                         if version else "This app has no App Store versions yet.")
        else:
            next_step = {
                "draft": "Editable draft. Run preflight_submission before submitting it.",
                "waiting_for_review": "Submitted and waiting for App Review. Nothing to do but wait.",
                "in_review": "App Review is looking at it now.",
                "rejected": "Rejected or pulled from review. Read Resolution Center, fix, and resubmit.",
                "approved": "Approved. Release it in App Store Connect if the release is manual.",
                "processing": "Approved and being processed for the store.",
                "live": "This version is live on the App Store.",
                "replaced": "Superseded by a newer version.",
            }.get(target.phase, f"State {target.app_version_state or target.app_store_state}.")
        return VersionStateReport(app_id=app, app_name=((app_raw or {}).get("attributes") or {}).get("name"),
                                  requested_version=version, version=target,
                                  recent_versions=self._mark_replaced([self._version(v, builds)
                                                                       for v in raw_versions[:5]]),
                                  open_review_submissions=open_subs, next_step=next_step)

    @staticmethod
    def _mark_replaced(versions: list[VersionSummary]) -> list[VersionSummary]:
        """Only the newest live version is on sale; Apple can still report older ones as
        READY_FOR_DISTRIBUTION, so label those 'replaced' (versions arrive newest first)."""
        seen_live = False
        for v in versions:
            if v.phase == "live":
                if seen_live:
                    v.phase = "replaced"
                seen_live = True
        return versions

    # -- lint_release_notes ------------------------------------------------------------------
    def lint_release_notes(self, text: str, field: str = "whats_new", locale: str | None = None) -> LintReport:
        return lint(text, field, self.rt.policy, locale)

    # -- preflight_submission ----------------------------------------------------------------
    async def preflight_submission(self, version: str, build_number: str | None = None,
                                   app_id: str | None = None, repo_path: str | None = None,
                                   commit: str | None = None, xcconfig_path: str | None = None,
                                   expected_iap_product_ids: list[str] | None = None) -> PreflightReport:
        app = self.app_id(app_id)
        asc = self.rt.asc()
        checks: list[CheckResult] = []
        add = checks.append
        ctx: dict[str, Any] = {}

        # 1. version record
        raw_versions, builds_by_id = await asc.versions(app, limit=8)
        target_raw = next((v for v in raw_versions if (v.get("attributes") or {}).get("versionString") == version),
                          None)
        if target_raw is None:
            filtered, more = await asc.versions(app, version=version, limit=1)
            builds_by_id.update(more)
            target_raw = filtered[0] if filtered else None
        has_live = any((version_state(v.get("attributes") or {}) or "") in LIVE_STATES
                       or (v.get("attributes") or {}).get("appStoreState") in LIVE_STATES
                       for v in raw_versions if v is not target_raw)
        if target_raw is None:
            add(CheckResult(id="version_exists", title="App Store version exists", status="fail",
                            detail=f"No iOS App Store version {version} for app {app}.",
                            fix=f"Create version {version} in App Store Connect (Distribution > + Version)."))
        else:
            vs = self._version(target_raw, builds_by_id)
            ctx["version"] = vs
            add(CheckResult(id="version_exists", title="App Store version exists", status="pass",
                            detail=f"{version} is {vs.app_version_state or vs.app_store_state}.",
                            evidence={"version_id": vs.version_id}))
            state = vs.app_version_state or vs.app_store_state or ""
            if vs.editable:
                add(CheckResult(id="version_editable", title="Version is editable", status="pass",
                                detail=f"{state} accepts a build, metadata and submission."))
            else:
                add(CheckResult(id="version_editable", title="Version is editable", status="fail",
                                detail=f"{state} ({vs.phase}) cannot be submitted.",
                                fix=("Already in the review pipeline; wait, or remove it from review in App "
                                     "Store Connect before changing it.") if vs.phase in
                                ("waiting_for_review", "in_review") else
                                "Create a new version; this one is past the submission stage."))

        # 2. build
        raw_builds = await asc.builds(app, version, limit=20)
        builds = [self._build(b) for b in raw_builds]
        attached_num = ctx["version"].attached_build_number if "version" in ctx else None
        want = build_number or attached_num
        selected = next((b for b in builds if b.build_number == want), None) if want else \
            next((b for b in builds if b.processing_state == "VALID" and not b.expired), builds[0] if builds else None)
        ctx["build"] = selected
        if selected is None:
            add(CheckResult(id="build_valid", title="Build processed and VALID", status="fail",
                            detail=f"No build {want or ''} uploaded for {version}.".replace("  ", " "),
                            fix="Archive and upload the build (Xcode Organizer or `xcrun altool`/Transporter)."))
        elif selected.expired:
            add(CheckResult(id="build_valid", title="Build processed and VALID", status="fail",
                            detail=f"Build {selected.build_number} has expired.", fix="Upload a new build."))
        elif selected.processing_state != "VALID":
            add(CheckResult(id="build_valid", title="Build processed and VALID", status="fail",
                            detail=f"Build {selected.build_number} is {selected.processing_state}.",
                            fix="Wait for processing to finish." if selected.processing_state == "PROCESSING"
                            else "Fix the processing error from Apple's email, bump the build number, re-upload.",
                            evidence={"build_id": selected.build_id}))
        else:
            add(CheckResult(id="build_valid", title="Build processed and VALID", status="pass",
                            detail=f"Build {selected.build_number} is VALID.",
                            evidence={"build_id": selected.build_id, "uploaded": selected.uploaded_date}))
        if selected is not None and "version" in ctx:
            if attached_num == selected.build_number:
                add(CheckResult(id="build_attached", title="Build attached to version", status="pass",
                                detail=f"Build {attached_num} is attached to {version}."))
            else:
                add(CheckResult(id="build_attached", title="Build attached to version", status="warn",
                                detail=(f"{version} has build {attached_num} attached, not {selected.build_number}."
                                        if attached_num else f"No build is attached to {version} yet."),
                                fix=f"Attach build {selected.build_number} to {version} before submitting "
                                    "(submit_for_review plans this step)."))

        # 3. export compliance
        if selected is not None:
            add(self._export_compliance(selected, repo_path))

        # 4. What's New
        if "version" in ctx:
            try:
                locs = await asc.localizations(target_raw["id"])  # type: ignore[index]
                checks.extend(self._whats_new_checks(locs, has_live))
            except ApiError as err:
                add(self._skipped("whats_new_present", "What's New present", err))

        # 5. App Review notes and contact
        if "version" in ctx:
            try:
                detail = await asc.review_detail(target_raw["id"])  # type: ignore[index]
                checks.extend(self._review_detail_checks(detail))
            except ApiError as err:
                add(self._skipped("review_notes_present", "App Review notes present", err))

        # 6. In-app purchases waiting to ride along
        try:
            add(await self._iap_check(app, expected_iap_product_ids or []))
        except ApiError as err:
            add(self._skipped("iap_ready_to_submit", "In-app purchases attached", err))

        # 7. conflicting review submissions
        if "version" in ctx:
            try:
                add(await self._submission_conflicts(app, target_raw["id"]))  # type: ignore[index]
            except ApiError as err:
                add(self._skipped("no_conflicting_submission", "No other version in review", err))

        # 8. local repo
        checks.extend(self._repo_checks(version, builds, selected, repo_path, commit, xcconfig_path))

        failed = [c.id for c in checks if c.status == "fail"]
        warned = sum(1 for c in checks if c.status == "warn")
        verdict = "blocked" if failed else ("ready_with_warnings" if warned else "ready")
        log("preflight_verdict", verdict=verdict, failed=failed, warned=warned)
        return PreflightReport(app_id=app, version=version,
                               build_number=selected.build_number if selected else build_number,
                               verdict=verdict, passed=sum(1 for c in checks if c.status == "pass"),
                               failed=len(failed), warned=warned,
                               skipped=sum(1 for c in checks if c.status == "skip"),
                               blocking=failed, checks=checks, generated_at=_now_iso())

    @staticmethod
    def _skipped(check_id: str, title: str, err: ApiError) -> CheckResult:
        if err.status in (401, 429) or err.status == 0:
            raise err
        return CheckResult(id=check_id, title=title, status="skip", detail=f"Could not check: {err}",
                           fix="Give the API key the App Manager role if this is a permissions error.")

    def _export_compliance(self, build: BuildInfo, repo_path: str | None) -> CheckResult:
        title = "Export compliance answered"
        if build.uses_non_exempt_encryption is False:
            return CheckResult(id="export_compliance", title=title, status="pass",
                               detail="Build declares no non-exempt encryption.")
        if build.uses_non_exempt_encryption is True:
            return CheckResult(id="export_compliance", title=title, status="warn",
                               detail="Build declares non-exempt encryption.",
                               fix="Make sure export compliance documentation is approved in App Store Connect.")
        plist_note = ""
        repo = Path(repo_path) if repo_path else self.rt.settings.repo_path
        if repo:
            plist = self._info_plist(repo)
            if plist is not None:
                value = gitrepo.plist_value(plist, "ITSAppUsesNonExemptEncryption")
                plist_note = (f" {plist.name} sets ITSAppUsesNonExemptEncryption={value}, so builds uploaded from "
                              "this tree answer it automatically." if value is not None else
                              f" {plist.name} does not set ITSAppUsesNonExemptEncryption.")
        return CheckResult(id="export_compliance", title=title, status="fail",
                           detail=f"Build {build.build_number} has no export compliance answer." + plist_note,
                           fix=("Answer export compliance for this build in App Store Connect (TestFlight > build), "
                                "and add ITSAppUsesNonExemptEncryption to Info.plist so future builds are "
                                "answered at upload."))

    def _info_plist(self, repo: Path) -> Path | None:
        explicit = self.rt.settings.info_plist_path
        if explicit:
            path = repo / explicit
            return path if path.is_file() else None
        for candidate in gitrepo.find_files(repo, "Info.plist", max_depth=3) + \
                sorted(repo.glob("*Info.plist")):
            if gitrepo.plist_value(candidate, "ITSAppUsesNonExemptEncryption") is not None:
                return candidate
        return None

    def _whats_new_checks(self, locs: list[dict[str, Any]], has_live: bool) -> list[CheckResult]:
        texts = {(l.get("attributes") or {}).get("locale") or "?": (l.get("attributes") or {}).get("whatsNew") or ""
                 for l in locs}
        empty = sorted(k for k, v in texts.items() if not v.strip())
        out: list[CheckResult] = []
        if not has_live:
            out.append(CheckResult(id="whats_new_present", title="What's New present", status="skip",
                                   detail="First release: App Store Connect does not take What's New yet."))
        elif empty:
            out.append(CheckResult(id="whats_new_present", title="What's New present", status="fail",
                                   detail=f"What's New is empty for {len(empty)} of {len(texts)} locales: "
                                          + ", ".join(empty[:8]) + ("…" if len(empty) > 8 else ""),
                                   fix="Write release notes for every locale (they can reuse the en-US text).",
                                   evidence={"empty_locales": empty}))
        else:
            out.append(CheckResult(id="whats_new_present", title="What's New present", status="pass",
                                   detail=f"Present in all {len(texts)} locales." if len(texts) != 1 else
                                   f"Present in the version's only locale ({next(iter(texts))})."))
        errors: dict[str, list[str]] = {}
        warnings: dict[str, list[str]] = {}
        for locale, text in texts.items():
            if not text.strip():
                continue
            report = lint(text, "whats_new", self.rt.policy, locale)
            for f in report.findings:
                bucket = errors if f.severity == "error" else warnings
                bucket.setdefault(f"{f.rule_id}: \"{f.match[:40]}\"", []).append(locale)
        if errors:
            items = [f"{k} ({', '.join(sorted(v)[:4])}{'…' if len(v) > 4 else ''})" for k, v in errors.items()]
            out.append(CheckResult(id="whats_new_policy", title="What's New free of banned claims", status="fail",
                                   detail="; ".join(items[:6]),
                                   fix="Rewrite the flagged phrases; run lint_release_notes on the new text.",
                                   evidence={"errors": errors, "warnings": warnings}))
        elif warnings:
            items = [f"{k} ({', '.join(sorted(v)[:4])})" for k, v in warnings.items()]
            out.append(CheckResult(id="whats_new_policy", title="What's New free of banned claims", status="warn",
                                   detail="; ".join(items[:6]), fix="Review the flagged wording.",
                                   evidence={"warnings": warnings}))
        elif any(t.strip() for t in texts.values()):
            out.append(CheckResult(id="whats_new_policy", title="What's New free of banned claims", status="pass",
                                   detail=f"No policy findings across {len(texts)} locales "
                                          f"(policy: {self.rt.policy.source})."))
        return out

    def _review_detail_checks(self, detail: dict[str, Any] | None) -> list[CheckResult]:
        if not detail:
            return [CheckResult(id="review_notes_present", title="App Review notes present", status="fail",
                                detail="The version has no App Review information.",
                                fix="Fill in App Review Information (contact, notes, demo account) in App Store "
                                    "Connect.")]
        a = detail.get("attributes") or {}
        notes = (a.get("notes") or "").strip()
        out = []
        if notes:
            report = lint(notes, "review_notes", self.rt.policy)
            status = "pass" if report.ok else "fail"
            out.append(CheckResult(id="review_notes_present", title="App Review notes present", status=status,
                                   detail=f"{len(notes)} characters of review notes."
                                   + ("" if report.ok else " " + "; ".join(f.message for f in report.findings
                                                                           if f.severity == "error")),
                                   fix=None if report.ok else "Shorten or fix the review notes."))
        else:
            out.append(CheckResult(id="review_notes_present", title="App Review notes present", status="fail",
                                   detail="App Review notes are empty.",
                                   fix="Explain how to reach paywalled or account-gated features, and anything "
                                       "the reviewer should know about this release."))
        contact_ok = bool(a.get("contactEmail")) and bool(a.get("contactPhone"))
        demo_ok = not a.get("demoAccountRequired") or bool(a.get("demoAccountName"))
        if contact_ok and demo_ok:
            out.append(CheckResult(id="review_contact", title="Reviewer contact and demo account", status="pass",
                                   detail="Contact details set" + (" and demo account provided."
                                                                   if a.get("demoAccountRequired") else ".")))
        else:
            problems = ([] if contact_ok else ["contact email/phone missing"]) + \
                       ([] if demo_ok else ["demo account required but not provided"])
            out.append(CheckResult(id="review_contact", title="Reviewer contact and demo account", status="fail",
                                   detail="; ".join(problems) + ".",
                                   fix="Complete App Review Information in App Store Connect."))
        return out

    async def _iap_check(self, app: str, expected: list[str]) -> CheckResult:
        asc = self.rt.asc()
        iaps, t1 = await asc.in_app_purchases(app)
        subs, t2 = await asc.subscriptions(app)
        products = [((p.get("attributes") or {}).get("productId") or p["id"], (p.get("attributes") or {}).get("state"),
                     "iap") for p in iaps] + \
                   [((s.get("attributes") or {}).get("productId") or s["id"], (s.get("attributes") or {}).get("state"),
                     "subscription") for s in subs]
        ready = sorted(pid for pid, state, _ in products if state == "READY_TO_SUBMIT")
        attached = sorted(pid for pid, state, _ in products if state in IAP_ATTACHED_STATES)
        blocked = sorted(f"{pid} ({state})" for pid, state, _ in products if state in IAP_BLOCKING_STATES)
        known = {pid for pid, _, _ in products}
        evidence = {"ready_to_submit": ready, "attached_or_in_review": attached, "needs_action": blocked,
                    "total_products": len(products), "truncated": t1 or t2}
        title = "In-app purchases attached"
        missing = sorted(set(expected) - known)
        unattached_expected = sorted(set(expected) & set(ready))
        if missing or unattached_expected:
            parts = ([f"not found: {', '.join(missing)}"] if missing else []) + \
                    ([f"READY_TO_SUBMIT but not attached: {', '.join(unattached_expected)}"]
                     if unattached_expected else [])
            return CheckResult(id="iap_ready_to_submit", title=title, status="fail", detail="; ".join(parts) + ".",
                               fix="Add them to this version (In-App Purchases and Subscriptions section on the "
                                   "version page) so App Review sees them with the binary.", evidence=evidence)
        if ready:
            return CheckResult(id="iap_ready_to_submit", title=title, status="warn",
                               detail=f"{len(ready)} product(s) are READY_TO_SUBMIT and not attached to any version: "
                                      + ", ".join(ready[:6]) + ("…" if len(ready) > 6 else ""),
                               fix="If they should ship with this release, add them on the version page before "
                                   "submitting; first-time products are only reviewed with an app version.",
                               evidence=evidence)
        if blocked:
            return CheckResult(id="iap_ready_to_submit", title=title, status="warn",
                               detail="Products needing action: " + ", ".join(blocked[:6]),
                               fix="Complete their metadata or review notes in App Store Connect.",
                               evidence=evidence)
        return CheckResult(id="iap_ready_to_submit", title=title, status="pass",
                           detail=f"No unattached products ({len(products)} checked, {len(attached)} attached or "
                                  "in review).", evidence=evidence)

    async def _submission_conflicts(self, app: str, version_id: str) -> CheckResult:
        asc = self.rt.asc()
        subs = await asc.review_submissions(app, limit=20)
        conflicts = []
        for sub in subs:
            state = (sub.get("attributes") or {}).get("state")
            if state not in OPEN_SUBMISSION_STATES:
                continue
            holds = await asc.submission_version_ids(sub["id"])
            if holds - {version_id}:
                conflicts.append(f"{self.rt.mask(sub['id'])} ({state})")
        if conflicts:
            return CheckResult(id="no_conflicting_submission", title="No other version in review", status="fail",
                               detail="Open review submission(s) hold another version: " + ", ".join(conflicts),
                               fix="Wait for that review to finish or remove it from review first.")
        return CheckResult(id="no_conflicting_submission", title="No other version in review", status="pass",
                           detail="No open review submission holds a different version.")

    def _repo_checks(self, version: str, builds: list[BuildInfo], selected: BuildInfo | None,
                     repo_path: str | None, commit: str | None, xcconfig_path: str | None) -> list[CheckResult]:
        repo = Path(repo_path).expanduser() if repo_path else self.rt.settings.repo_path
        if not repo:
            skip = "No repository configured (pass repo_path or set RELEASE_GUARD_REPO)."
            return [CheckResult(id="build_number_monotonic", title="Build number monotonic", status="skip",
                                detail=skip),
                    CheckResult(id="commit_on_main", title="Release commit is on main", status="skip", detail=skip)]
        out = [self._build_number_check(repo, version, builds, selected, xcconfig_path)]
        out.append(self._commit_check(repo, commit))
        return out

    def _build_number_check(self, repo: Path, version: str, builds: list[BuildInfo], selected: BuildInfo | None,
                            xcconfig_path: str | None) -> CheckResult:
        title = "Build number monotonic vs AppVersion.xcconfig"
        rel = xcconfig_path or self.rt.settings.xcconfig_path
        candidates = [repo / rel] if rel else gitrepo.find_files(repo, "AppVersion.xcconfig")
        candidates = [c for c in candidates if c.is_file()]
        if not candidates:
            return CheckResult(id="build_number_monotonic", title=title, status="skip",
                               detail="No AppVersion.xcconfig found.",
                               fix="Pass xcconfig_path (relative to the repo).")
        parsed = {str(c.relative_to(repo)): gitrepo.read_xcconfig(c) for c in candidates}
        pairs = {(v.get("MARKETING_VERSION"), v.get("CURRENT_PROJECT_VERSION")) for v in parsed.values()}
        if len(pairs) > 1:
            return CheckResult(id="build_number_monotonic", title=title, status="warn",
                               detail="xcconfig files disagree: " + "; ".join(
                                   f"{k}: {v.get('MARKETING_VERSION')} ({v.get('CURRENT_PROJECT_VERSION')})"
                                   for k, v in parsed.items()),
                               fix="Keep one source of truth, or pass xcconfig_path.")
        marketing, local_build = next(iter(pairs))
        source = next(iter(parsed))
        evidence = {"xcconfig": source, "MARKETING_VERSION": marketing, "CURRENT_PROJECT_VERSION": local_build}
        if not local_build:
            return CheckResult(id="build_number_monotonic", title=title, status="skip",
                               detail=f"{source} does not set CURRENT_PROJECT_VERSION.", evidence=evidence)
        if marketing and marketing != version:
            return CheckResult(id="build_number_monotonic", title=title, status="warn",
                               detail=f"Local tree is on {marketing} ({local_build}); checking {version}.",
                               fix="Check out the release commit, or bump MARKETING_VERSION.", evidence=evidence)
        uploaded = sorted((b.build_number for b in builds), key=gitrepo.version_tuple)
        highest = uploaded[-1] if uploaded else None
        evidence["highest_uploaded"] = highest
        lt = gitrepo.version_tuple(local_build)
        if highest is None:
            return CheckResult(id="build_number_monotonic", title=title, status="pass",
                               detail=f"No builds uploaded for {version} yet; local build is {local_build}.",
                               evidence=evidence)
        ht = gitrepo.version_tuple(highest)
        if lt < ht:
            return CheckResult(id="build_number_monotonic", title=title, status="fail",
                               detail=f"Local CURRENT_PROJECT_VERSION {local_build} is behind App Store Connect "
                                      f"({highest}). The next upload would be rejected.",
                               fix=f"Set CURRENT_PROJECT_VERSION to {ht[0] + 1 if len(ht) == 1 else highest + '.1'} "
                                   f"in {source}.", evidence=evidence)
        if selected and gitrepo.version_tuple(selected.build_number) < lt:
            return CheckResult(id="build_number_monotonic", title=title, status="warn",
                               detail=f"Submitting build {selected.build_number}, but the local tree is at "
                                      f"{local_build}.",
                               fix="Confirm you mean the older build, or upload the newer one.", evidence=evidence)
        return CheckResult(id="build_number_monotonic", title=title, status="pass",
                           detail=f"Local {marketing} ({local_build}) is not behind the newest upload ({highest}). "
                                  "Bump it before the next upload." if lt == ht else
                                  f"Local {local_build} is ahead of the newest upload ({highest}).",
                           evidence=evidence)

    def _commit_check(self, repo: Path, commit: str | None) -> CheckResult:
        title = "Release commit is on main"
        if not gitrepo.is_git_repo(repo):
            return CheckResult(id="commit_on_main", title=title, status="skip", detail=f"{repo.name} is not a git repo.")
        rev = commit or "HEAD"
        try:
            main_ref = self.rt.settings.main_ref
            refs = [main_ref] + (["main"] if main_ref != "main" else [])
            ancestry = gitrepo.commit_on_branch(repo, rev, refs)
        except gitrepo.RepoError as err:
            return CheckResult(id="commit_on_main", title=title, status="fail", detail=str(err),
                               fix="Pass a commit sha, tag or branch name.")
        evidence = {"commit": (ancestry.commit or "")[:12], "ref": ancestry.ref,
                    "ref_sha": (ancestry.ref_sha or "")[:12]}
        if ancestry.is_ancestor is None:
            return CheckResult(id="commit_on_main", title=title, status="skip", detail=ancestry.note,
                               fix="Run `git fetch origin` and retry.", evidence=evidence)
        if ancestry.is_ancestor:
            return CheckResult(id="commit_on_main", title=title, status="pass",
                               detail=f"{evidence['commit'][:8]} is contained in {ancestry.ref}.", evidence=evidence)
        return CheckResult(id="commit_on_main", title=title, status="fail",
                           detail=f"{evidence['commit'][:8]} is not in {ancestry.ref}: the binary would ship code "
                                  "that main does not have.",
                           fix=f"Merge and push the release commit to {ancestry.ref.split('/')[-1]}, then rebuild "
                               "from it.", evidence=evidence)

    # -- reconcile_server_notifications ------------------------------------------------------
    async def reconcile_server_notifications(self, days: int = 14, environment: str = "production",
                                             scope: str = "failures_only", max_pages: int = 25,
                                             sample_limit: int = 10) -> ReconcileReport:
        cap = 180 if environment == "production" else 30
        if not 1 <= days <= cap:
            raise ValueError(f"days must be between 1 and {cap} for {environment}")
        api = self.rt.server_api(environment)
        end = dt.datetime.now(dt.UTC)
        start = end - dt.timedelta(days=days)
        items, pages, truncated = await api.history(int(start.timestamp() * 1000), int(end.timestamp() * 1000),
                                                    only_failures=scope == "failures_only", max_pages=max_pages)
        by_day: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
        by_type: collections.Counter[str] = collections.Counter()
        by_reason: collections.Counter[str] = collections.Counter()
        samples: list[UndeliveredNotification] = []
        ok = 0
        for item in items:
            payload = decode_signed_payload(item.get("signedPayload") or "")
            signed_ms = payload.get("signedDate")
            day = (dt.datetime.fromtimestamp(signed_ms / 1000, dt.UTC).strftime("%Y-%m-%d")
                   if isinstance(signed_ms, (int, float)) else "unknown")
            if delivered(item):
                ok += 1
                by_day[day]["delivered"] += 1
                continue
            by_day[day]["undelivered"] += 1
            ntype = payload.get("notificationType") or "UNKNOWN"
            by_type[ntype] += 1
            attempts = item.get("sendAttempts") or []
            last = attempts[-1].get("sendAttemptResult") if attempts else None
            by_reason[last or "NO_ATTEMPT_RECORDED"] += 1
            if len(samples) < sample_limit:
                samples.append(UndeliveredNotification(
                    notification_uuid=self.rt.mask(payload.get("notificationUUID")) or "?",
                    notification_type=ntype, subtype=payload.get("subtype"),
                    signed_date=dt.datetime.fromtimestamp(signed_ms / 1000, dt.UTC).isoformat()
                    if isinstance(signed_ms, (int, float)) else None,
                    attempts=len(attempts), last_result=last))
        undelivered = len(items) - ok
        if undelivered:
            next_step = (f"{undelivered} notification(s) never reached your server. Check your webhook's health "
                         "on the listed days, then replay them with your own tooling; this tool only reports.")
        else:
            next_step = "Every notification in the window reached your server."
        if truncated:
            next_step += f" Scan stopped at {pages} pages; raise max_pages or shorten days for full coverage."
        return ReconcileReport(
            environment=environment, window_days=days, start=start.isoformat(), end=end.isoformat(),
            scope=scope, scanned=len(items), delivered=ok, undelivered=undelivered,
            delivery_rate=round(ok / len(items), 4) if (items and scope == "all") else None,
            pages=pages, truncated=truncated,
            by_day=[DayCount(date=d, delivered=c["delivered"], undelivered=c["undelivered"])
                    for d, c in sorted(by_day.items())],
            by_type=dict(by_type.most_common()), by_failure_reason=dict(by_reason.most_common()),
            samples=samples, next_step=next_step)

    # -- submit_for_review -------------------------------------------------------------------
    async def submit_for_review(self, version: str, build_number: str, app_id: str | None = None,
                                confirm: bool = False, dry_run: bool = True,
                                repo_path: str | None = None, commit: str | None = None) -> SubmitReport:
        app = self.app_id(app_id)
        pre = await self.preflight_submission(version, build_number, app, repo_path=repo_path, commit=commit)
        asc = self.rt.asc()
        vs = next((c for c in pre.checks if c.id == "version_exists" and c.status == "pass"), None)
        plan: list[PlannedWrite] = []
        version_id = build_id = None
        if vs is not None:
            raw_versions, _ = await asc.versions(app, version=version, limit=1)
            version_raw = raw_versions[0]
            version_id = version_raw["id"]
            raw_builds = await asc.builds(app, version, build_number=build_number, limit=5)
            build_id = raw_builds[0]["id"] if raw_builds else None
            attached = (((version_raw.get("relationships") or {}).get("build") or {}).get("data") or {}).get("id")
            if build_id and attached != build_id:
                plan.append(PlannedWrite(method="PATCH",
                                         path=f"/v1/appStoreVersions/{self.rt.mask(version_id)}/relationships/build",
                                         description=f"Attach build {build_number} to {version}"))
            plan.append(PlannedWrite(method="POST", path="/v1/reviewSubmissions",
                                     description="Create (or reuse the draft) review submission for iOS"))
            plan.append(PlannedWrite(method="POST", path="/v1/reviewSubmissionItems",
                                     description=f"Add version {version} to the submission"))
            plan.append(PlannedWrite(method="PATCH", path="/v1/reviewSubmissions/{id}",
                                     description="Set submitted=true (sends it to App Review)"))

        def report(mode: str, reason: str | None, next_step: str, executed: list[PlannedWrite] | None = None,
                   final: str | None = None) -> SubmitReport:
            return SubmitReport(mode=mode, version=version, build_number=build_number,  # type: ignore[arg-type]
                                preflight_verdict=pre.verdict, blocking_checks=pre.blocking, planned_writes=plan,
                                executed_writes=executed or [], refused_reason=reason, final_state=final,
                                next_step=next_step)

        s = self.rt.settings
        if dry_run:
            gate_note = ("Preflight is blocked: fix " + ", ".join(pre.blocking) + " first."
                         if pre.blocking else
                         "Show this plan to the user. Only if they explicitly approve, call again with "
                         "dry_run=false and confirm=true.")
            return report("dry_run", None, "Dry run: nothing was changed. " + gate_note)
        if not confirm:
            return report("refused", "confirm=true is required to submit",
                          "Ask the user to confirm the plan, then pass confirm=true.")
        if not s.allow_writes:
            return report("refused", "server is read-only (RELEASE_GUARD_ALLOW_WRITES is not set)",
                          "Writes are disabled for this server. The user can submit in App Store Connect, or "
                          "restart the server with RELEASE_GUARD_ALLOW_WRITES=1.")
        if running_under_test():
            return report("refused", "writes are disabled under test runners", "No action.")
        if s.backend != "live":
            return report("refused", "demo backend never writes", "No action.")
        if pre.blocking or not version_id or not build_id:
            return report("refused", "preflight is blocked: " + ", ".join(pre.blocking or ["missing version/build"]),
                          "Fix the blocking checks, then run the dry run again.")

        permit = issue_permit(f"submit {version} ({build_number}) confirmed by user")
        executed: list[PlannedWrite] = []
        log("write_path_entered", level=30, version=version, build_number=build_number)
        if plan and plan[0].path.endswith("/relationships/build"):
            await asc.attach_build(version_id, build_id, permit)
            executed.append(plan[0])
        subs = await asc.review_submissions(app, limit=20)
        drafts = [x for x in subs if (x.get("attributes") or {}).get("state") == "READY_FOR_REVIEW"]
        submission_id = None
        for draft in drafts:
            holds = await asc.submission_version_ids(draft["id"])
            if holds <= {version_id}:
                submission_id, has_item = draft["id"], version_id in holds
                break
        if submission_id is None:
            submission_id, has_item = await asc.create_submission(app, permit), False
            executed.append(plan[-3])
        if not has_item:
            await asc.add_submission_item(submission_id, version_id, permit)
            executed.append(plan[-2])
        await asc.submit_submission(submission_id, permit)
        executed.append(plan[-1])
        after, _ = await asc.versions(app, version=version, limit=1)
        final = version_state((after[0] if after else {}).get("attributes") or {})
        return report("submitted", None, "Submitted. Track it with check_version_state.", executed, final)


__all__ = ["EDITABLE_STATES", "ReleaseGuard"]
