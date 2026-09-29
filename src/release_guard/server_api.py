"""App Store Server API: notification history (report-only delivery audit).

Apple exposes history as POST /inApps/v1/notifications/history. It reads and
changes nothing, so the transport allowlists exactly that path as a query POST.
Replaying notifications is out of scope on purpose (see docs/SECURITY.md).
"""

from __future__ import annotations

import base64
import json
from typing import Any

from .transport import ResilientClient

HISTORY_PATH = "/inApps/v1/notifications/history"
PAGE_SIZE = 20  # Apple returns up to 20 records per call.


def decode_signed_payload(signed_payload: str) -> dict[str, Any]:
    """Decode the JWS payload for reporting. Not a signature check: the data came from
    Apple over TLS on an authenticated call and is only counted, never acted on."""
    try:
        segment = signed_payload.split(".")[1]
        segment += "=" * (-len(segment) % 4)
        return json.loads(base64.urlsafe_b64decode(segment))
    except (IndexError, ValueError):
        return {}


def delivered(item: dict[str, Any]) -> bool:
    return any(a.get("sendAttemptResult") == "SUCCESS" for a in item.get("sendAttempts") or [])


class AppStoreServerApi:
    def __init__(self, client: ResilientClient) -> None:
        self.client = client

    async def history(self, start_ms: int, end_ms: int, *, only_failures: bool,
                      max_pages: int) -> tuple[list[dict[str, Any]], int, bool]:
        body: dict[str, Any] = {"startDate": start_ms, "endDate": end_ms}
        if only_failures:
            body["onlyFailures"] = True
        items: list[dict[str, Any]] = []
        token: str | None = None
        pages = 0
        while True:
            params = {"paginationToken": token} if token else None
            page = await self.client.request("POST", HISTORY_PATH, params=params, json=body)
            pages += 1
            items.extend(page.get("notificationHistory") or [])
            if not page.get("hasMore"):
                return items, pages, False
            if pages >= max_pages:
                return items, pages, True
            token = page.get("paginationToken")
            if not token:
                return items, pages, False
