import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

BASE_URL_ENV = "USER_SERVICE_URL"
REQUEST_TIMEOUT_SECONDS = 2.0
CACHE_TTL_SECONDS = 600
OUTAGE_BACKOFF_SECONDS = 30
MAX_CACHE_ENTRIES = 5000
CHUNK_SIZE = 100
DATA_FIELDS = ("original_data", "updated_data")

USER_ID_KEYS = frozenset({
    "created_by_id", "modified_by_id", "createdbyid", "modifiedbyid", "doctorid", "doctor2id",
    "supervisorid", "supervisor2id", "caregiverid", "gametherapistid", "tempdoctorid",
    "tempcaregiverid", "guardianapplicationuserid", "administeredbyid",
})

_cache: Dict[str, Tuple[float, Optional[str]]] = {}
_outage_until = 0.0


def reset_cache() -> None:
    global _outage_until
    _cache.clear()
    _outage_until = 0.0


def _name_key(key: str) -> Optional[str]:
    if key.endswith("_id"):
        return key[:-3] + "_name"
    if key.endswith("Id") or key.endswith("ID"):
        return key[:-2] + "Name"
    return None


def _get(row: Any, field: str) -> Any:
    if isinstance(row, dict):
        return row.get(field)
    return getattr(row, field, None)


def _user_id_items(data: Any):
    if not isinstance(data, dict):
        return
    for key, value in data.items():
        if isinstance(key, str) and key.lower() in USER_ID_KEYS and isinstance(value, str) and value:
            yield key, value


def _fetch(base_url: str, token: str, ids: List[str]) -> Dict[str, str]:
    names: Dict[str, str] = {}
    for start in range(0, len(ids), CHUNK_SIZE):
        chunk = ids[start:start + CHUNK_SIZE]
        response = httpx.post(
            f"{base_url}/api/v1/user/names",
            json={"ids": chunk},
            headers={"Authorization": f"Bearer {token}"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        body = response.json()["names"]
        if not isinstance(body, dict):
            raise ValueError("names is not an object")
        for user_id, name in body.items():
            if isinstance(name, str) and name:
                names[str(user_id)] = name
    return names


def _annotate(data: Any, names: Dict[str, str]) -> Any:
    if not isinstance(data, dict):
        return data
    additions = {}
    for key, value in _user_id_items(data):
        name_key = _name_key(key)
        name = names.get(value)
        if name_key and name and name_key not in data:
            additions[name_key] = name
    return {**data, **additions} if additions else data


def resolve_user_names(logs: List[Any], token: str) -> List[Any]:
    global _outage_until
    try:
        base_url = os.getenv(BASE_URL_ENV, "").rstrip("/")
        if not base_url or not token or not logs:
            return logs

        wanted = []
        for row in logs:
            for field in DATA_FIELDS:
                for _, value in _user_id_items(_get(row, field)):
                    if value not in wanted:
                        wanted.append(value)
        if not wanted:
            return logs

        now = time.monotonic()
        names: Dict[str, str] = {}
        missing = []
        for user_id in wanted:
            cached = _cache.get(user_id)
            if cached and cached[0] > now:
                if cached[1]:
                    names[user_id] = cached[1]
            else:
                missing.append(user_id)

        if missing and now >= _outage_until:
            try:
                fetched = _fetch(base_url, token, missing)
            except Exception:
                _outage_until = now + OUTAGE_BACKOFF_SECONDS
                raise
            for user_id in missing:
                if user_id not in _cache and len(_cache) >= MAX_CACHE_ENTRIES:
                    _cache.clear()
                _cache[user_id] = (now + CACHE_TTL_SECONDS, fetched.get(user_id))
            names.update(fetched)

        if not names:
            return logs

        result = []
        for row in logs:
            updates = {}
            for field in DATA_FIELDS:
                original = _get(row, field)
                annotated = _annotate(original, names)
                if annotated is not original:
                    updates[field] = annotated
            if not updates:
                result.append(row)
            elif isinstance(row, dict):
                result.append({**row, **updates})
            else:
                result.append(row.model_copy(update=updates))
        return result
    except Exception as exc:
        logger.warning("User name resolution failed; returning logs unchanged: %s", exc)
        return logs
