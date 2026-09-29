"""An in-process fake of the App Store Connect API and App Store Server API.

Used by the test suite, the eval harness and `RELEASE_GUARD_BACKEND=demo`, so
nothing in CI or in a demo ever calls Apple. Responses follow Apple's documented
JSON:API shapes (data / attributes / relationships / included / links.next),
cross-checked against live read-only responses during verification.
"""

from __future__ import annotations

import base64
import copy
import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

DAY_MS = 86_400_000


def _jws(payload: dict[str, Any]) -> str:
    def seg(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{seg({'alg': 'ES256', 'x5c': ['fake']})}.{seg(payload)}.ZmFrZXNpZw"


@dataclass
class Fault:
    """Return `status` for the next `count` requests whose path starts with `path_prefix`."""

    path_prefix: str
    status: int
    count: int = 1
    headers: dict[str, str] = field(default_factory=dict)
    body: dict[str, Any] | None = None


@dataclass
class Scenario:
    app: dict[str, Any]
    versions: list[dict[str, Any]]
    builds: list[dict[str, Any]]
    localizations: dict[str, list[dict[str, Any]]]
    review_details: dict[str, dict[str, Any] | None]
    iaps: list[dict[str, Any]]
    subscription_groups: list[dict[str, Any]]
    subscriptions: dict[str, list[dict[str, Any]]]
    review_submissions: list[dict[str, Any]]
    submission_items: dict[str, list[str]]
    notifications: list[dict[str, Any]]
    faults: list[Fault] = field(default_factory=list)
    rate_limit: tuple[int, int] = (3600, 3500)


def _res(type_: str, id_: str, **attributes: Any) -> dict[str, Any]:
    return {"type": type_, "id": id_, "attributes": attributes,
            "links": {"self": f"https://api.appstoreconnect.apple.com/v1/{type_}/{id_}"}}


def demo_scenario(now_ms: int | None = None) -> Scenario:
    """A plausible app mid-release: 2.4.0 draft, build 42 VALID, build 43 processing."""
    now_ms = now_ms or int(time.time() * 1000)
    app = _res("apps", "1234567890", name="Demo Fitness", bundleId="com.example.demofitness")
    v240 = _res("appStoreVersions", "ver-240-0000-aaaa", versionString="2.4.0",
                appVersionState="PREPARE_FOR_SUBMISSION", appStoreState="PREPARE_FOR_SUBMISSION",
                releaseType="AFTER_APPROVAL", createdDate="2026-09-20T10:00:00.000-07:00")
    v240["relationships"] = {"build": {"data": None}}
    v231 = _res("appStoreVersions", "ver-231-0000-bbbb", versionString="2.3.1",
                appVersionState="READY_FOR_DISTRIBUTION", appStoreState="READY_FOR_SALE",
                releaseType="AFTER_APPROVAL", createdDate="2026-09-01T10:00:00.000-07:00")
    v231["relationships"] = {"build": {"data": {"type": "builds", "id": "bld-41-0000-cccc"}}}
    v230 = _res("appStoreVersions", "ver-230-0000-dddd", versionString="2.3.0",
                appVersionState="REPLACED_WITH_NEW_VERSION", appStoreState="REPLACED_WITH_NEW_VERSION",
                releaseType="AFTER_APPROVAL", createdDate="2026-08-15T10:00:00.000-07:00")
    v230["relationships"] = {"build": {"data": {"type": "builds", "id": "bld-40-0000-eeee"}}}
    builds = [
        dict(_res("builds", "bld-43-0000-ffff", version="43", processingState="PROCESSING",
                  uploadedDate="2026-09-28T09:40:00.000-07:00", expired=False, usesNonExemptEncryption=None,
                  minOsVersion="17.0"), _marketing="2.4.0"),
        dict(_res("builds", "bld-42-0000-gggg", version="42", processingState="VALID",
                  uploadedDate="2026-09-27T16:05:00.000-07:00", expired=False, usesNonExemptEncryption=None,
                  minOsVersion="17.0"), _marketing="2.4.0"),
        dict(_res("builds", "bld-41-0000-cccc", version="41", processingState="VALID",
                  uploadedDate="2026-09-01T09:00:00.000-07:00", expired=False, usesNonExemptEncryption=False,
                  minOsVersion="17.0"), _marketing="2.3.1"),
        dict(_res("builds", "bld-40-0000-eeee", version="40", processingState="VALID",
                  uploadedDate="2026-08-15T09:00:00.000-07:00", expired=True, usesNonExemptEncryption=False,
                  minOsVersion="17.0"), _marketing="2.3.0"),
    ]
    localizations = {
        "ver-240-0000-aaaa": [
            _res("appStoreVersionLocalizations", "loc-en", locale="en-US",
                 whatsNew="Workout streaks and faster sync.\nNew members get 20% off - subscribe on our website!"),
            _res("appStoreVersionLocalizations", "loc-de", locale="de-DE", whatsNew=""),
            _res("appStoreVersionLocalizations", "loc-fr", locale="fr-FR",
                 whatsNew="Séries d'entraînement et synchronisation plus rapide."),
        ],
        "ver-231-0000-bbbb": [
            _res("appStoreVersionLocalizations", "loc-en-231", locale="en-US", whatsNew="Bug fixes."),
        ],
    }
    review_details = {
        "ver-240-0000-aaaa": _res("appStoreReviewDetails", "rev-240", notes="Use the demo account to reach Pro "
                                  "features. Streaks are under Profile > Streaks.", demoAccountRequired=True,
                                  demoAccountName="reviewer@example.com", contactEmail="release@example.com",
                                  contactPhone="+1 555 0100"),
    }
    iaps = [
        _res("inAppPurchases", "6700000001", productId="com.example.demofitness.coins.100", name="100 Coins",
             state="APPROVED", inAppPurchaseType="CONSUMABLE"),
        _res("inAppPurchases", "6700000002", productId="com.example.demofitness.coins.500", name="500 Coins",
             state="READY_TO_SUBMIT", inAppPurchaseType="CONSUMABLE"),
    ]
    groups = [_res("subscriptionGroups", "2100000001", referenceName="Pro")]
    subscriptions = {"2100000001": [
        _res("subscriptions", "6700000101", productId="com.example.demofitness.pro.monthly", name="Pro Monthly",
             state="APPROVED"),
        _res("subscriptions", "6700000102", productId="com.example.demofitness.pro.yearly", name="Pro Yearly",
             state="READY_TO_SUBMIT"),
    ]}
    submissions = [_res("reviewSubmissions", "sub-231-0000", state="COMPLETE", platform="IOS",
                        submittedDate="2026-09-01T12:00:00.000-07:00")]
    items = {"sub-231-0000": ["ver-231-0000-bbbb"]}
    notifications = []
    types = ["DID_RENEW", "SUBSCRIBED", "DID_CHANGE_RENEWAL_STATUS", "ONE_TIME_CHARGE", "EXPIRED"]
    for i in range(40):
        signed = now_ms - (i * 8 + 3) * 3_600_000
        failed = i in (5, 6, 21)
        attempts = ([{"attemptDate": signed + k * 60_000, "sendAttemptResult": r}
                     for k, r in enumerate(["TIMED_OUT", "TIMED_OUT", "UNSUCCESSFUL_HTTP_RESPONSE_CODE"]
                                           if i != 21 else ["NO_RESPONSE"])]
                    if failed else [{"attemptDate": signed + 1000, "sendAttemptResult": "SUCCESS"}])
        if i == 30:  # failed once, then Apple's retry succeeded
            attempts = [{"attemptDate": signed + 1000, "sendAttemptResult": "TIMED_OUT"},
                        {"attemptDate": signed + 60_000, "sendAttemptResult": "SUCCESS"}]
        notifications.append({
            "signedPayload": _jws({"notificationType": types[i % len(types)],
                                   "subtype": "AUTO_RENEW_DISABLED" if i % 5 == 2 else None,
                                   "notificationUUID": f"0000{i:04d}-demo-4c3b-9a1e-00000000{i:04d}",
                                   "signedDate": signed, "version": "2.0"}),
            "sendAttempts": attempts,
        })
    return Scenario(app=app, versions=[v240, v231, v230], builds=builds, localizations=localizations,
                    review_details=review_details, iaps=iaps, subscription_groups=groups,
                    subscriptions=subscriptions, review_submissions=submissions, submission_items=items,
                    notifications=notifications)


class FakeApple:
    """httpx handler serving a Scenario. Records every request for assertions."""

    def __init__(self, scenario: Scenario | None = None) -> None:
        self.s = scenario or demo_scenario()
        self.requests: list[httpx.Request] = []
        self.require_auth = False

    # -- plumbing ----------------------------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _json(self, status: int, body: Any, headers: dict[str, str] | None = None) -> httpx.Response:
        h = {"x-rate-limit": f"user-hour-lim:{self.s.rate_limit[0]};user-hour-rem:{self.s.rate_limit[1]};",
             "x-request-id": f"FAKE-{len(self.requests):04d}"}
        h.update(headers or {})
        return httpx.Response(status, json=body, headers=h)

    @staticmethod
    def _err(status: int, code: str, detail: str) -> dict[str, Any]:
        return {"errors": [{"status": str(status), "code": code, "title": code.replace("_", " ").title(),
                            "detail": detail}]}

    def _page(self, request: httpx.Request, data: list[dict[str, Any]], params: dict[str, str],
              included: list[dict[str, Any]] | None = None) -> httpx.Response:
        limit = int(params.get("limit", "50"))
        offset = int(params.get("cursor", "0"))
        chunk = data[offset:offset + limit]
        body: dict[str, Any] = {"data": copy.deepcopy(chunk), "links": {"self": str(request.url)},
                                "meta": {"paging": {"total": len(data), "limit": limit}}}
        if included is not None:
            body["included"] = included
        if offset + limit < len(data):
            nxt = request.url.copy_merge_params({"cursor": str(offset + limit)})
            body["links"]["next"] = str(nxt)
        for item in body["data"]:
            item.pop("_marketing", None)
        return self._json(200, body)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        for fault in self.s.faults:
            if fault.count > 0 and path.startswith(fault.path_prefix):
                fault.count -= 1
                body = fault.body or self._err(fault.status, "RATE_LIMIT_EXCEEDED" if fault.status == 429
                                               else "SERVER_ERROR", "injected fault")
                return self._json(fault.status, body, fault.headers)
        if self.require_auth and not request.headers.get("authorization", "").startswith("Bearer "):
            return self._json(401, self._err(401, "NOT_AUTHORIZED", "missing token"))
        params = {k: v[-1] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}
        if request.method == "POST" and path == "/inApps/v1/notifications/history":
            return self._history(request, params)
        if request.method != "GET":
            return self._json(405, self._err(405, "METHOD_NOT_ALLOWED", "the fake backend is read-only"))
        return self._get(request, path, params)

    # -- App Store Connect -------------------------------------------------------------------
    def _get(self, request: httpx.Request, path: str, params: dict[str, str]) -> httpx.Response:
        s = self.s
        parts = [p for p in path.split("/") if p]
        if parts[:2] == ["v1", "apps"] and len(parts) == 3:
            if parts[2] != s.app["id"]:
                return self._json(404, self._err(404, "NOT_FOUND", "no app"))
            return self._json(200, {"data": s.app})
        if parts[:2] == ["v1", "apps"] and len(parts) == 4:
            if parts[2] != s.app["id"]:
                return self._json(404, self._err(404, "NOT_FOUND", "no app"))
            kind = parts[3]
            if kind == "appStoreVersions":
                versions = s.versions
                if "filter[versionString]" in params:
                    versions = [v for v in versions
                                if v["attributes"]["versionString"] == params["filter[versionString]"]]
                included = None
                if params.get("include") == "build":
                    ids = set()
                    for v in versions:
                        ref = ((v.get("relationships") or {}).get("build") or {}).get("data")
                        if ref:
                            ids.add(ref["id"])
                    included = [{k: val for k, val in b.items() if k != "_marketing"}
                                for b in s.builds if b["id"] in ids]
                return self._page(request, versions, params, included)
            if kind == "inAppPurchasesV2":
                return self._page(request, s.iaps, params)
            if kind == "subscriptionGroups":
                return self._page(request, s.subscription_groups, params)
        if path == "/v1/builds":
            if params.get("filter[app]") != s.app["id"]:
                return self._page(request, [], params)
            builds = [b for b in s.builds
                      if b["_marketing"] == params.get("filter[preReleaseVersion.version]", b["_marketing"])]
            if "filter[version]" in params:
                builds = [b for b in builds if b["attributes"]["version"] == params["filter[version]"]]
            builds = sorted(builds, key=lambda b: b["attributes"]["uploadedDate"], reverse=True)
            return self._page(request, builds, params)
        if parts[:2] == ["v1", "appStoreVersions"] and len(parts) == 4:
            vid, kind = parts[2], parts[3]
            if kind == "appStoreVersionLocalizations":
                return self._page(request, s.localizations.get(vid, []), params)
            if kind == "appStoreReviewDetail":
                detail = s.review_details.get(vid)
                if detail is None:
                    return self._json(200, {"data": None})
                return self._json(200, {"data": detail})
        if parts[:2] == ["v1", "subscriptionGroups"] and len(parts) == 4:
            return self._page(request, s.subscriptions.get(parts[2], []), params)
        if path == "/v1/reviewSubmissions":
            subs = s.review_submissions if params.get("filter[app]") == s.app["id"] else []
            return self._page(request, subs, params)
        if parts[:2] == ["v1", "reviewSubmissions"] and len(parts) == 4 and parts[3] == "items":
            items = [{"type": "reviewSubmissionItems", "id": f"item-{vid}", "attributes": {"state": "READY_FOR_REVIEW"},
                      "relationships": {"appStoreVersion": {"data": {"type": "appStoreVersions", "id": vid}}}}
                     for vid in s.submission_items.get(parts[2], [])]
            return self._page(request, items, params)
        return self._json(404, self._err(404, "NOT_FOUND", f"fake has no route for {path}"))

    # -- App Store Server API ----------------------------------------------------------------
    def _history(self, request: httpx.Request, params: dict[str, str]) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        start, end = body.get("startDate", 0), body.get("endDate", 2**62)
        items = []
        for n in self.s.notifications:
            payload = json.loads(base64.urlsafe_b64decode(n["signedPayload"].split(".")[1] + "=="))
            if not start <= payload["signedDate"] <= end:
                continue
            if body.get("onlyFailures") and all(a["sendAttemptResult"] == "SUCCESS" for a in n["sendAttempts"]):
                continue
            items.append(n)
        offset = int(params.get("paginationToken", "0"))
        chunk = items[offset:offset + 20]
        more = offset + 20 < len(items)
        out: dict[str, Any] = {"notificationHistory": chunk, "hasMore": more}
        if more:
            out["paginationToken"] = str(offset + 20)
        return self._json(200, out)


def demo_transports() -> tuple[httpx.MockTransport, httpx.MockTransport]:
    fake = FakeApple()
    return fake.transport(), fake.transport()
