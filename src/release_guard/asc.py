"""Typed read operations on the App Store Connect API (plus the guarded writes).

Every read asks only for the fields it needs (`fields[...]`). Review details are
fetched without the demo-account password, and the notes body never leaves this
module except as a length and lint result.
"""

from __future__ import annotations

from typing import Any

from .transport import ResilientClient, WritePermit

EDITABLE_STATES = {"PREPARE_FOR_SUBMISSION", "DEVELOPER_REJECTED", "REJECTED", "METADATA_REJECTED",
                   "INVALID_BINARY"}
LIVE_STATES = {"READY_FOR_SALE", "READY_FOR_DISTRIBUTION"}
OPEN_SUBMISSION_STATES = {"READY_FOR_REVIEW", "WAITING_FOR_REVIEW", "IN_REVIEW", "UNRESOLVED_ISSUES",
                          "CANCELING", "COMPLETING"}
IAP_BLOCKING_STATES = {"MISSING_METADATA", "DEVELOPER_ACTION_NEEDED", "REJECTED", "WAITING_FOR_UPLOAD"}
IAP_ATTACHED_STATES = {"WAITING_FOR_REVIEW", "IN_REVIEW", "PENDING_BINARY_APPROVAL"}

_PHASES = {
    "PREPARE_FOR_SUBMISSION": "draft", "READY_FOR_REVIEW": "draft",
    "WAITING_FOR_REVIEW": "waiting_for_review", "WAITING_FOR_EXPORT_COMPLIANCE": "draft",
    "IN_REVIEW": "in_review",
    "REJECTED": "rejected", "METADATA_REJECTED": "rejected", "DEVELOPER_REJECTED": "rejected",
    "INVALID_BINARY": "rejected",
    "PENDING_DEVELOPER_RELEASE": "approved", "PENDING_APPLE_RELEASE": "approved", "ACCEPTED": "approved",
    "PROCESSING_FOR_DISTRIBUTION": "processing", "PROCESSING_FOR_APP_STORE": "processing",
    "READY_FOR_DISTRIBUTION": "live", "READY_FOR_SALE": "live",
    "REPLACED_WITH_NEW_VERSION": "replaced",
}


def version_state(attrs: dict[str, Any]) -> str | None:
    return attrs.get("appVersionState") or attrs.get("appStoreState")


def phase_of(attrs: dict[str, Any]) -> str:
    state = attrs.get("appVersionState") or ""
    store = attrs.get("appStoreState") or ""
    return _PHASES.get(state) or _PHASES.get(store) or "other"


def is_editable(attrs: dict[str, Any]) -> bool:
    return (version_state(attrs) or "") in EDITABLE_STATES or (attrs.get("appStoreState") or "") in EDITABLE_STATES


BUILD_FIELDS = "version,processingState,uploadedDate,expired,usesNonExemptEncryption,minOsVersion"


class AppStoreConnect:
    def __init__(self, client: ResilientClient) -> None:
        self.client = client

    async def app(self, app_id: str) -> dict[str, Any] | None:
        body = await self.client.get_optional(f"/v1/apps/{app_id}", {"fields[apps]": "name,bundleId"})
        return (body or {}).get("data")

    async def builds(self, app_id: str, version: str, build_number: str | None = None,
                     limit: int = 10) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "filter[app]": app_id,
            "filter[preReleaseVersion.version]": version,
            "sort": "-uploadedDate",
            "limit": limit,
            "fields[builds]": BUILD_FIELDS,
        }
        if build_number:
            params["filter[version]"] = build_number
        return (await self.client.get("/v1/builds", params)).get("data") or []

    async def versions(self, app_id: str, version: str | None = None,
                       limit: int = 5) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        params: dict[str, Any] = {
            "filter[platform]": "IOS",
            "limit": limit,
            "include": "build",
            "fields[appStoreVersions]": "versionString,appVersionState,appStoreState,releaseType,createdDate,build",
            "fields[builds]": "version,processingState,usesNonExemptEncryption,expired",
        }
        if version:
            params["filter[versionString]"] = version
        body = await self.client.get(f"/v1/apps/{app_id}/appStoreVersions", params)
        builds = {b["id"]: b for b in body.get("included") or [] if b.get("type") == "builds"}
        data = sorted(body.get("data") or [], key=lambda v: (v.get("attributes") or {}).get("createdDate") or "",
                      reverse=True)
        return data, builds

    async def localizations(self, version_id: str) -> list[dict[str, Any]]:
        data, _, _ = await self.client.paginate(
            f"/v1/appStoreVersions/{version_id}/appStoreVersionLocalizations",
            {"limit": 50, "fields[appStoreVersionLocalizations]": "locale,whatsNew"})
        return data

    async def review_detail(self, version_id: str) -> dict[str, Any] | None:
        # demoAccountPassword is deliberately not requested.
        body = await self.client.get_optional(
            f"/v1/appStoreVersions/{version_id}/appStoreReviewDetail",
            {"fields[appStoreReviewDetails]": "notes,demoAccountRequired,demoAccountName,contactEmail,contactPhone"})
        return (body or {}).get("data")

    async def in_app_purchases(self, app_id: str) -> tuple[list[dict[str, Any]], bool]:
        data, _, truncated = await self.client.paginate(
            f"/v1/apps/{app_id}/inAppPurchasesV2",
            {"limit": 200, "fields[inAppPurchases]": "productId,name,state,inAppPurchaseType"}, max_pages=5)
        return data, truncated

    async def subscriptions(self, app_id: str) -> tuple[list[dict[str, Any]], bool]:
        groups, _, truncated = await self.client.paginate(
            f"/v1/apps/{app_id}/subscriptionGroups",
            {"limit": 50, "fields[subscriptionGroups]": "referenceName"}, max_pages=3)
        subs: list[dict[str, Any]] = []
        for group in groups:
            data, _, more = await self.client.paginate(
                f"/v1/subscriptionGroups/{group['id']}/subscriptions",
                {"limit": 200, "fields[subscriptions]": "productId,name,state"}, max_pages=3)
            subs.extend(data)
            truncated = truncated or more
        return subs, truncated

    async def review_submissions(self, app_id: str, limit: int = 20) -> list[dict[str, Any]]:
        body = await self.client.get("/v1/reviewSubmissions", {
            "filter[app]": app_id, "filter[platform]": "IOS", "limit": limit,
            "fields[reviewSubmissions]": "state,submittedDate,platform"})
        return body.get("data") or []

    async def submission_version_ids(self, submission_id: str) -> set[str]:
        body = await self.client.get(f"/v1/reviewSubmissions/{submission_id}/items", {
            "limit": 50, "include": "appStoreVersion", "fields[appStoreVersions]": "versionString"})
        ids = set()
        for item in body.get("data") or []:
            ref = ((item.get("relationships") or {}).get("appStoreVersion") or {}).get("data") or {}
            if ref.get("id"):
                ids.add(ref["id"])
        return ids

    # -- guarded writes (need a WritePermit; the transport refuses otherwise) -------------
    async def set_export_compliance(self, build_id: str, uses_non_exempt: bool, permit: WritePermit) -> None:
        await self.client.request("PATCH", f"/v1/builds/{build_id}", permit=permit, json={
            "data": {"type": "builds", "id": build_id,
                     "attributes": {"usesNonExemptEncryption": uses_non_exempt}}})

    async def attach_build(self, version_id: str, build_id: str, permit: WritePermit) -> None:
        await self.client.request("PATCH", f"/v1/appStoreVersions/{version_id}/relationships/build",
                                  permit=permit, json={"data": {"type": "builds", "id": build_id}})

    async def create_submission(self, app_id: str, permit: WritePermit) -> str:
        body = await self.client.request("POST", "/v1/reviewSubmissions", permit=permit, json={
            "data": {"type": "reviewSubmissions", "attributes": {"platform": "IOS"},
                     "relationships": {"app": {"data": {"type": "apps", "id": app_id}}}}})
        return body["data"]["id"]

    async def add_submission_item(self, submission_id: str, version_id: str, permit: WritePermit) -> None:
        await self.client.request("POST", "/v1/reviewSubmissionItems", permit=permit, json={
            "data": {"type": "reviewSubmissionItems", "relationships": {
                "reviewSubmission": {"data": {"type": "reviewSubmissions", "id": submission_id}},
                "appStoreVersion": {"data": {"type": "appStoreVersions", "id": version_id}}}}})

    async def submit_submission(self, submission_id: str, permit: WritePermit) -> None:
        await self.client.request("PATCH", f"/v1/reviewSubmissions/{submission_id}", permit=permit, json={
            "data": {"type": "reviewSubmissions", "id": submission_id, "attributes": {"submitted": True}}})
