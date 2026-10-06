import hashlib
import os
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

import httpx

BASE_URL_ENV = "USER_SERVICE_URL"
CACHE_TTL_SECONDS = 60
OUTAGE_BACKOFF_SECONDS = 30
REQUEST_TIMEOUT_SECONDS = 2.0
MAX_CACHE_ENTRIES = 10000


@dataclass(frozen=True)
class VerifiedUser:
    userId: str
    fullName: str
    roleName: str
    email: str


class Verdict:
    VALID = "valid"
    REJECTED = "rejected"
    UNAVAILABLE = "unavailable"
    UNCONFIGURED = "unconfigured"


_cache: Dict[str, Tuple[float, str, Optional[VerifiedUser]]] = {}
_outage_until = 0.0


def reset_cache() -> None:
    global _outage_until
    _cache.clear()
    _outage_until = 0.0


def _call_user_service(base_url: str, token: str) -> httpx.Response:
    return httpx.get(
        f"{base_url}/api/v1/current_user/",
        headers={"Authorization": f"Bearer {token}"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


def verify_token(token: str, now: Optional[Callable[[], float]] = None) -> Tuple[str, Optional[VerifiedUser]]:
    global _outage_until
    clock = now or time.monotonic
    base_url = os.getenv(BASE_URL_ENV, "").rstrip("/")
    if not base_url:
        return Verdict.UNCONFIGURED, None

    current = clock()
    if current < _outage_until:
        return Verdict.UNAVAILABLE, None

    key = hashlib.sha256(token.encode("utf-8")).hexdigest()
    cached = _cache.get(key)
    if cached and cached[0] > current:
        return cached[1], cached[2]

    try:
        response = _call_user_service(base_url, token)
    except httpx.HTTPError:
        _outage_until = current + OUTAGE_BACKOFF_SECONDS
        return Verdict.UNAVAILABLE, None

    if response.status_code == 200:
        body = response.json()
        result = (
            Verdict.VALID,
            VerifiedUser(
                userId=str(body["userId"]),
                fullName=body["fullName"],
                roleName=body["roleName"],
                email=body.get("email", ""),
            ),
        )
    elif response.status_code in (401, 403, 404):
        result = (Verdict.REJECTED, None)
    else:
        _outage_until = current + OUTAGE_BACKOFF_SECONDS
        return Verdict.UNAVAILABLE, None

    if len(_cache) >= MAX_CACHE_ENTRIES:
        _cache.clear()
    _cache[key] = (current + CACHE_TTL_SECONDS, result[0], result[1])
    return result
