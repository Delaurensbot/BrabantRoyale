"""Server-side clan policy and leader-decision storage.

T13 deliberately keeps this layer separate from the T07--T12 domain code.
Policy values are a small, validated read model that can be consumed by
future analytics/monitor code.  Leader decisions are an append-only audit
model and are never part of a public response.

The browser only ever reaches this module through the API adapters in
``api/clan_policy.py`` and ``api/leader_decisions.py``.  Writes and private
reads use the existing ``X-Analytics-Admin-Key`` RLS boundary; no Supabase
secret key is accepted from a request.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

import requests

try:
    from supabase_history import (
        DEFAULT_SUPABASE_URL,
        _supabase_headers,
        get_supabase_read_config,
        normalize_tag,
    )
except ImportError:  # pragma: no cover - convenient for loose Vercel files.
    from .supabase_history import (  # type: ignore
        DEFAULT_SUPABASE_URL,
        _supabase_headers,
        get_supabase_read_config,
        normalize_tag,
    )


POLICY_TABLE = "clan_policy_settings"
LEADER_DECISIONS_TABLE = "leader_decisions"
ANALYTICS_ADMIN_KEY_HEADER = "X-Analytics-Admin-Key"
ADMIN_KEY_HEADER = ANALYTICS_ADMIN_KEY_HEADER

POLICY_FIELDS = (
    "duel_first_enabled",
    "duel_first_alert_after_utc",
    "promotion_window_weeks",
    "promotion_min_average",
    "promotion_min_reliability",
    "promotion_min_observed_races",
    "demotion_window_weeks",
    "demotion_max_missed_attacks",
    "trial_races_required",
)

# These defaults preserve the existing analytics thresholds where they
# already existed, while failing closed for the new Duel-first switch until a
# leader explicitly enables it.  The values are also repeated in the SQL
# migration so a missing row and a missing column have the same meaning.
DEFAULT_CLAN_POLICY: Dict[str, object] = {
    "duel_first_enabled": False,
    "duel_first_alert_after_utc": "12:00:00Z",
    "promotion_window_weeks": 6,
    "promotion_min_average": 2500,
    "promotion_min_reliability": 95.0,
    "promotion_min_observed_races": 2,
    "demotion_window_weeks": 10,
    "demotion_max_missed_attacks": 2,
    "trial_races_required": 2,
}
# A descriptive alias is useful to callers and keeps the default immutable by
# convention without requiring a custom mapping type.
DEFAULT_POLICY = DEFAULT_CLAN_POLICY

DECISION_TYPES = frozenset(
    {
        "promotion",
        "demotion",
        "exemption",
        "strategic_experiment",
        "main_clan",
        "BR2",
        "BR3",
        "reject",
        "manual_correction",
    }
)
_DECISION_TYPE_BY_INPUT = {value.lower(): value for value in DECISION_TYPES}

MAX_ACTOR_LENGTH = 120
MAX_REASON_LENGTH = 240
MAX_RACE_KEY_LENGTH = 256
MAX_IDEMPOTENCY_KEY_LENGTH = 128
MAX_DECISION_LIMIT = 100
DEFAULT_DECISION_LIMIT = 50
DEFAULT_STORAGE_TIMEOUT = 25.0
DEFAULT_STORAGE_MAX_RETRIES = 2
DEFAULT_STORAGE_BACKOFF_SECONDS = 0.25
DEFAULT_STORAGE_MAX_BACKOFF_SECONDS = 2.0

_TAG_PATTERN = re.compile(r"[A-Z0-9]{1,32}\Z")
_INTEGER_PATTERN = re.compile(r"[+-]?\d+\Z")
_IDEMPOTENCY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_TIME_PATTERN = re.compile(
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?"
)


class ClanPolicyStorageError(RuntimeError):
    """Safe metadata for a policy/decision storage failure."""

    def __init__(
        self,
        code: str,
        *,
        status_code: Optional[int] = None,
        attempts: int = 0,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.attempts = attempts
        super().__init__(_safe_storage_message(code))


def _safe_storage_message(code: str) -> str:
    return {
        "configuration_error": "Policy storage configuration is invalid.",
        "timeout": "Policy storage request timed out.",
        "transport_error": "Policy storage request failed temporarily.",
        "rate_limited": "Policy storage is rate limited.",
        "upstream_server_error": "Policy storage is temporarily unavailable.",
        "supabase_http_error": "Policy storage request was rejected.",
        "forbidden": "Admin authorization was rejected.",
        "invalid_response": "Policy storage returned an invalid response.",
        "invalid_payload": "Policy storage payload is invalid.",
    }.get(code, "Policy storage request failed.")


def _configured_secret_values() -> Iterable[str]:
    """Yield configured secrets without retaining them in result payloads."""

    for env_name in (
        "CLASH_ROYALE_API_KEY",
        "SUPABASE_SECRET_KEY",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SUPABASE_PUBLISHABLE_KEY",
        "SUPABASE_ANON_KEY",
        "SUPABASE_INGEST_TOKEN",
        "WAR_MONITOR_SECRET",
        "WAR_STATUS_LEADER_SECRET",
    ):
        value = os.environ.get(env_name, "").strip()
        if value:
            yield value


def _contains_configured_secret(value: str) -> bool:
    return any(secret in value for secret in _configured_secret_values())


def validate_clan_tag(value: object) -> str:
    """Return a canonical Clash tag without silently selecting a fallback clan."""

    candidate = normalize_tag(value)
    if not _TAG_PATTERN.fullmatch(candidate) or _contains_configured_secret(candidate):
        raise ValueError("Invalid clan tag.")
    return candidate


def _safe_text(
    value: object,
    field: str,
    *,
    maximum: int,
    required: bool = True,
) -> Optional[str]:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text.")
    candidate = value.strip()
    if not candidate and not required:
        return None
    if not candidate:
        raise ValueError(f"{field} is required.")
    if len(candidate) > maximum:
        raise ValueError(f"{field} is too long.")
    if any(ord(character) < 32 or ord(character) == 127 for character in candidate):
        raise ValueError(f"{field} contains invalid control characters.")
    if _contains_configured_secret(candidate):
        raise ValueError(f"{field} is invalid.")
    return candidate


def _safe_integer(
    value: object,
    field: str,
    *,
    minimum: int,
    maximum: int,
    allowed: Optional[Iterable[int]] = None,
) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be an integer.")
    if isinstance(value, int):
        candidate = value
    elif isinstance(value, str) and _INTEGER_PATTERN.fullmatch(value.strip()):
        try:
            candidate = int(value.strip())
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"{field} must be an integer.") from None
    elif isinstance(value, float) and math.isfinite(value) and value.is_integer():
        candidate = int(value)
    else:
        raise ValueError(f"{field} must be an integer.")
    if candidate < minimum or candidate > maximum:
        raise ValueError(f"{field} is outside the allowed range.")
    if allowed is not None and candidate not in set(allowed):
        raise ValueError(f"{field} has an unsupported value.")
    return candidate


def _safe_reliability(value: object) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError("promotion_min_reliability must be a number.")
    try:
        candidate = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("promotion_min_reliability must be a number.") from None
    if not math.isfinite(candidate) or candidate < 0 or candidate > 100:
        raise ValueError("promotion_min_reliability is outside the allowed range.")
    rounded = round(candidate, 2)
    if abs(candidate - rounded) > 1e-9:
        raise ValueError("promotion_min_reliability may have at most two decimals.")
    return rounded


def _safe_alert_time(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("duel_first_alert_after_utc must be a UTC time.")
    candidate = value.strip().upper()
    if candidate.endswith("Z"):
        candidate = candidate[:-1]
    match = _TIME_PATTERN.fullmatch(candidate)
    if not match:
        raise ValueError("duel_first_alert_after_utc must be a UTC time.")
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    second = int(match.group("second") or 0)
    if hour > 23 or minute > 59 or second > 59:
        raise ValueError("duel_first_alert_after_utc must be a UTC time.")
    return f"{hour:02d}:{minute:02d}:{second:02d}Z"


def _default_policy_for(clan_tag: str) -> Dict[str, object]:
    return {"clan_tag": clan_tag, **dict(DEFAULT_CLAN_POLICY)}


def validate_policy_payload(payload: Mapping[str, object]) -> Dict[str, object]:
    """Validate a policy request and fill omitted fields with explicit defaults."""

    if not isinstance(payload, Mapping):
        raise ValueError("Policy request must be a JSON object.")
    clan_tag = validate_clan_tag(payload.get("clan_tag"))
    values: Dict[str, object] = {"clan_tag": clan_tag}

    raw = payload.get("duel_first_enabled", DEFAULT_CLAN_POLICY["duel_first_enabled"])
    if not isinstance(raw, bool):
        raise ValueError("duel_first_enabled must be boolean.")
    values["duel_first_enabled"] = raw

    values["duel_first_alert_after_utc"] = _safe_alert_time(
        payload.get(
            "duel_first_alert_after_utc",
            DEFAULT_CLAN_POLICY["duel_first_alert_after_utc"],
        )
    )
    values["promotion_window_weeks"] = _safe_integer(
        payload.get(
            "promotion_window_weeks",
            DEFAULT_CLAN_POLICY["promotion_window_weeks"],
        ),
        "promotion_window_weeks",
        minimum=2,
        maximum=6,
        allowed=(2, 4, 6),
    )
    values["promotion_min_average"] = _safe_integer(
        payload.get(
            "promotion_min_average",
            DEFAULT_CLAN_POLICY["promotion_min_average"],
        ),
        "promotion_min_average",
        minimum=0,
        maximum=10000,
    )
    values["promotion_min_reliability"] = _safe_reliability(
        payload.get(
            "promotion_min_reliability",
            DEFAULT_CLAN_POLICY["promotion_min_reliability"],
        )
    )
    values["promotion_min_observed_races"] = _safe_integer(
        payload.get(
            "promotion_min_observed_races",
            DEFAULT_CLAN_POLICY["promotion_min_observed_races"],
        ),
        "promotion_min_observed_races",
        minimum=1,
        maximum=52,
    )
    values["demotion_window_weeks"] = _safe_integer(
        payload.get(
            "demotion_window_weeks",
            DEFAULT_CLAN_POLICY["demotion_window_weeks"],
        ),
        "demotion_window_weeks",
        minimum=1,
        maximum=52,
    )
    values["demotion_max_missed_attacks"] = _safe_integer(
        payload.get(
            "demotion_max_missed_attacks",
            DEFAULT_CLAN_POLICY["demotion_max_missed_attacks"],
        ),
        "demotion_max_missed_attacks",
        minimum=0,
        maximum=832,
    )
    values["trial_races_required"] = _safe_integer(
        payload.get(
            "trial_races_required",
            DEFAULT_CLAN_POLICY["trial_races_required"],
        ),
        "trial_races_required",
        minimum=1,
        maximum=52,
    )
    return values


def _normalise_policy_row(
    row: Mapping[str, object], clan_tag: str
) -> Dict[str, object]:
    """Normalize a trusted DB row; absent columns use the explicit defaults."""

    payload: Dict[str, object] = {"clan_tag": clan_tag}
    for field in POLICY_FIELDS:
        if field in row and row[field] is not None:
            payload[field] = row[field]
        else:
            payload[field] = DEFAULT_CLAN_POLICY[field]
    return validate_policy_payload(payload)


def _timestamp_to_iso(value: object, field: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise ValueError(f"{field} is invalid.")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"{field} is invalid.") from None
    else:
        raise ValueError(f"{field} is invalid.")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _optional_timestamp(value: object, field: str) -> Optional[str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return _timestamp_to_iso(value, field)


def validate_decision_payload(
    payload: Mapping[str, object],
    *,
    clock: Optional[Callable[[], object]] = None,
) -> Dict[str, object]:
    """Validate one audit decision and produce a complete storage row."""

    if not isinstance(payload, Mapping):
        raise ValueError("Decision request must be a JSON object.")
    clan_tag = validate_clan_tag(payload.get("clan_tag"))

    player_value = payload.get("player_tag")
    player_tag = (
        None
        if player_value is None
        or (isinstance(player_value, str) and not player_value.strip())
        else validate_clan_tag(player_value)
    )

    decision_value = _safe_text(
        payload.get("decision_type"),
        "decision_type",
        maximum=64,
    )
    decision_type = _DECISION_TYPE_BY_INPUT.get((decision_value or "").lower())
    if decision_type is None:
        raise ValueError("decision_type is unsupported.")

    actor = _safe_text(
        payload.get("actor"),
        "actor",
        maximum=MAX_ACTOR_LENGTH,
    )
    reason = _safe_text(
        payload.get("reason"),
        "reason",
        maximum=MAX_REASON_LENGTH,
    )
    related_race_key = _safe_text(
        payload.get("related_race_key"),
        "related_race_key",
        maximum=MAX_RACE_KEY_LENGTH,
        required=False,
    )

    raw_key = payload.get("idempotency_key")
    if raw_key is None or (isinstance(raw_key, str) and not raw_key.strip()):
        idempotency_key = "generated-" + os.urandom(16).hex()
    else:
        if not isinstance(raw_key, str) or not _IDEMPOTENCY_PATTERN.fullmatch(
            raw_key.strip()
        ):
            raise ValueError("idempotency_key is invalid.")
        idempotency_key = raw_key.strip()

    raw_created_at = payload.get("created_at")
    if raw_created_at is None:
        try:
            now_value = clock() if clock is not None else datetime.now(timezone.utc)
        except Exception:
            now_value = datetime.now(timezone.utc)
        created_at = _timestamp_to_iso(now_value, "created_at")
    else:
        created_at = _timestamp_to_iso(raw_created_at, "created_at")

    return {
        "clan_tag": clan_tag,
        "player_tag": player_tag,
        "actor": actor,
        "decision_type": decision_type,
        "reason": reason,
        "related_race_key": related_race_key,
        "created_at": created_at,
        "idempotency_key": idempotency_key,
    }


def _normalise_decision_row(
    row: Mapping[str, object], clan_tag: str
) -> Optional[Dict[str, object]]:
    """Return only a valid row belonging to the requested clan."""

    try:
        if validate_clan_tag(row.get("clan_tag")) != clan_tag:
            return None
        player_value = row.get("player_tag")
        player_tag = (
            None
            if player_value is None
            or (isinstance(player_value, str) and not player_value.strip())
            else validate_clan_tag(player_value)
        )
        decision_type_value = _safe_text(
            row.get("decision_type"),
            "decision_type",
            maximum=64,
        )
        decision_type = _DECISION_TYPE_BY_INPUT.get((decision_type_value or "").lower())
        if decision_type is None:
            return None
        actor = _safe_text(row.get("actor"), "actor", maximum=MAX_ACTOR_LENGTH)
        reason = _safe_text(row.get("reason"), "reason", maximum=MAX_REASON_LENGTH)
        related_race_key = _safe_text(
            row.get("related_race_key"),
            "related_race_key",
            maximum=MAX_RACE_KEY_LENGTH,
            required=False,
        )
        created_at = _timestamp_to_iso(row.get("created_at"), "created_at")
        raw_key = row.get("idempotency_key")
        if not isinstance(raw_key, str) or not _IDEMPOTENCY_PATTERN.fullmatch(
            raw_key.strip()
        ):
            return None
    except ValueError:
        return None

    result: Dict[str, object] = {
        "clan_tag": clan_tag,
        "player_tag": player_tag,
        "actor": actor,
        "decision_type": decision_type,
        "reason": reason,
        "related_race_key": related_race_key,
        "created_at": created_at,
        "idempotency_key": raw_key.strip(),
    }
    raw_id = row.get("id")
    if isinstance(raw_id, int) and not isinstance(raw_id, bool) and raw_id >= 0:
        result["id"] = raw_id
    return result


def _clean_server_url(value: object) -> str:
    url = str(value or "").strip().rstrip("/")
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ClanPolicyStorageError("configuration_error")
    return url


def _resolve_storage_config(
    *,
    supabase_url: Optional[str],
    api_key: Optional[str],
) -> Tuple[str, str]:
    if api_key is not None and not isinstance(api_key, str):
        raise ClanPolicyStorageError("configuration_error")
    selected_key = api_key.strip() if isinstance(api_key, str) else ""
    if not selected_key:
        configured = get_supabase_read_config()
        if not configured:
            raise ClanPolicyStorageError("configuration_error")
        configured_url, configured_key = configured
        selected_key = str(configured_key or "").strip()
        selected_url = str(supabase_url or configured_url or "").strip()
    else:
        selected_url = str(supabase_url or os.environ.get("SUPABASE_URL", "")).strip()
        if not selected_url:
            selected_url = DEFAULT_SUPABASE_URL
    if not selected_key:
        raise ClanPolicyStorageError("configuration_error")
    return _clean_server_url(selected_url), selected_key


def _validate_request_options(
    *,
    timeout: float,
    max_retries: int,
    backoff_factor: float,
    max_backoff: float,
) -> None:
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or timeout <= 0
    ):
        raise ValueError("timeout must be a positive finite number.")
    if (
        isinstance(max_retries, bool)
        or not isinstance(max_retries, int)
        or max_retries < 0
        or max_retries > 3
    ):
        raise ValueError("max_retries must be between 0 and 3.")
    for name, value in (
        ("backoff_factor", backoff_factor),
        ("max_backoff", max_backoff),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value < 0
        ):
            raise ValueError(f"{name} must be a finite non-negative number.")
    if backoff_factor > max_backoff:
        raise ValueError("backoff_factor must not exceed max_backoff.")


def _storage_request(
    method: str,
    endpoint: str,
    *,
    headers: Mapping[str, str],
    params: Optional[Mapping[str, str]] = None,
    payload: Optional[Mapping[str, object]] = None,
    timeout: float,
    max_retries: int,
    backoff_factor: float,
    max_backoff: float,
    sleep: Optional[Callable[[float], None]],
) -> Tuple[Any, int]:
    _validate_request_options(
        timeout=timeout,
        max_retries=max_retries,
        backoff_factor=backoff_factor,
        max_backoff=max_backoff,
    )
    sleep_fn = sleep or time.sleep
    for attempt in range(1, max_retries + 2):
        try:
            if method.upper() == "GET":
                response = requests.get(
                    endpoint,
                    params=dict(params or {}),
                    headers=dict(headers),
                    timeout=timeout,
                )
            elif method.upper() == "POST":
                response = requests.post(
                    endpoint,
                    json=dict(payload or {}),
                    headers=dict(headers),
                    timeout=timeout,
                )
            else:
                raise ClanPolicyStorageError("configuration_error", attempts=attempt)
        except requests.exceptions.Timeout:
            if attempt <= max_retries:
                delay = min(max_backoff, backoff_factor * (2 ** (attempt - 1)))
                if delay > 0:
                    sleep_fn(delay)
                continue
            raise ClanPolicyStorageError("timeout", attempts=attempt) from None
        except requests.exceptions.RequestException:
            if attempt <= max_retries:
                delay = min(max_backoff, backoff_factor * (2 ** (attempt - 1)))
                if delay > 0:
                    sleep_fn(delay)
                continue
            raise ClanPolicyStorageError("transport_error", attempts=attempt) from None
        except ClanPolicyStorageError:
            raise
        except Exception:
            raise ClanPolicyStorageError("transport_error", attempts=attempt) from None

        try:
            status_code = int(response.status_code)
        except (TypeError, ValueError, AttributeError):
            raise ClanPolicyStorageError("invalid_response", attempts=attempt) from None

        if 200 <= status_code < 300:
            return response, attempt
        if status_code in (401, 403):
            raise ClanPolicyStorageError(
                "forbidden",
                status_code=status_code,
                attempts=attempt,
            )

        retryable = status_code == 429 or 500 <= status_code <= 599
        if retryable and attempt <= max_retries:
            delay = min(max_backoff, backoff_factor * (2 ** (attempt - 1)))
            if delay > 0:
                sleep_fn(delay)
            continue
        code = (
            "rate_limited"
            if status_code == 429
            else "upstream_server_error"
            if 500 <= status_code <= 599
            else "supabase_http_error"
        )
        raise ClanPolicyStorageError(
            code,
            status_code=status_code,
            attempts=attempt,
        ) from None

    raise ClanPolicyStorageError("transport_error", attempts=max_retries + 1)


def _admin_headers(
    api_key: str,
    admin_key: Optional[str],
    *,
    write: bool = False,
    prefer: Optional[str] = None,
) -> Dict[str, str]:
    headers = _supabase_headers(api_key, write=write)
    if admin_key:
        headers[ANALYTICS_ADMIN_KEY_HEADER] = admin_key
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _policy_result(
    clan_tag: str,
    policy: Mapping[str, object],
    *,
    status: str,
    source: str,
    ok: bool = True,
    error: Optional[str] = None,
    status_code: Optional[int] = None,
    attempts: Optional[int] = None,
    metadata: Optional[Mapping[str, object]] = None,
) -> Dict[str, object]:
    normalised = {"clan_tag": clan_tag}
    normalised.update({field: policy[field] for field in POLICY_FIELDS})
    result: Dict[str, object] = {
        "ok": ok,
        "status": status,
        "data_status": status,
        "source": source,
        "policy_source": source,
        "clan_tag": clan_tag,
        "policy": normalised,
    }
    # Top-level fields keep the route convenient for existing server callers;
    # the nested `policy` object is the documented canonical response shape.
    result.update({field: normalised[field] for field in POLICY_FIELDS})
    if error:
        result["error"] = error
    if status_code is not None:
        result["status_code"] = status_code
    if attempts is not None:
        result["attempts"] = attempts
    if metadata:
        for key, value in metadata.items():
            if value is not None:
                result[key] = value
    return result


def read_clan_policy(
    clan_tag: object,
    *,
    supabase_url: Optional[str] = None,
    api_key: Optional[str] = None,
    admin_key: Optional[str] = None,
    timeout: float = DEFAULT_STORAGE_TIMEOUT,
    max_retries: int = DEFAULT_STORAGE_MAX_RETRIES,
    backoff_factor: float = DEFAULT_STORAGE_BACKOFF_SECONDS,
    max_backoff: float = DEFAULT_STORAGE_MAX_BACKOFF_SECONDS,
    sleep: Optional[Callable[[float], None]] = None,
) -> Dict[str, object]:
    """Read one clan policy; absent rows/columns resolve to explicit defaults."""

    normalized_clan_tag = validate_clan_tag(clan_tag)
    defaults = _default_policy_for(normalized_clan_tag)
    try:
        configured_url, configured_key = _resolve_storage_config(
            supabase_url=supabase_url,
            api_key=api_key,
        )
        endpoint = f"{configured_url}/rest/v1/{POLICY_TABLE}"
        response, attempts = _storage_request(
            "GET",
            endpoint,
            headers=_admin_headers(configured_key, admin_key),
            params={
                "select": "clan_tag,"
                + ",".join(POLICY_FIELDS)
                + ",created_at,updated_at",
                "clan_tag": f"eq.{normalized_clan_tag}",
                "limit": "1",
            },
            timeout=timeout,
            max_retries=max_retries,
            backoff_factor=backoff_factor,
            max_backoff=max_backoff,
            sleep=sleep,
        )
        content = getattr(response, "content", b"")
        rows = response.json() if content else []
        if not isinstance(rows, list):
            raise ClanPolicyStorageError("invalid_response", attempts=attempts)
        if not rows:
            return _policy_result(
                normalized_clan_tag,
                defaults,
                status="defaults",
                source="defaults",
                attempts=attempts,
            )
        if not isinstance(rows[0], Mapping):
            raise ClanPolicyStorageError("invalid_response", attempts=attempts)
        row = dict(rows[0])
        row_clan = validate_clan_tag(row.get("clan_tag"))
        if row_clan != normalized_clan_tag:
            # A server-side filter must not be trusted as the only isolation
            # boundary when a proxy or fixture returns a broader row.
            return _policy_result(
                normalized_clan_tag,
                defaults,
                status="defaults",
                source="defaults",
                attempts=attempts,
            )
        policy = _normalise_policy_row(row, normalized_clan_tag)
        metadata = {
            "attempts": attempts,
            "created_at": _optional_timestamp(row.get("created_at"), "created_at"),
            "updated_at": _optional_timestamp(row.get("updated_at"), "updated_at"),
        }
        return _policy_result(
            normalized_clan_tag,
            policy,
            status="stored",
            source="stored",
            attempts=attempts,
            metadata=metadata,
        )
    except ClanPolicyStorageError as error:
        return _policy_result(
            normalized_clan_tag,
            defaults,
            status="error",
            source="defaults",
            ok=False,
            error=error.code,
            status_code=error.status_code,
            attempts=error.attempts,
        )
    except (TypeError, ValueError, OverflowError, json.JSONDecodeError):
        return _policy_result(
            normalized_clan_tag,
            defaults,
            status="error",
            source="defaults",
            ok=False,
            error="invalid_response",
        )


def load_clan_policy(*args: Any, **kwargs: Any) -> Dict[str, object]:
    """Descriptive alias for server-side consumers."""

    return read_clan_policy(*args, **kwargs)


def get_clan_policy(*args: Any, **kwargs: Any) -> Dict[str, object]:
    """Compatibility alias for server-side consumers."""

    return read_clan_policy(*args, **kwargs)


def write_clan_policy(
    payload: Mapping[str, object],
    admin_key: object,
    *,
    supabase_url: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: float = DEFAULT_STORAGE_TIMEOUT,
    max_retries: int = DEFAULT_STORAGE_MAX_RETRIES,
    backoff_factor: float = DEFAULT_STORAGE_BACKOFF_SECONDS,
    max_backoff: float = DEFAULT_STORAGE_MAX_BACKOFF_SECONDS,
    sleep: Optional[Callable[[float], None]] = None,
) -> Dict[str, object]:
    """Idempotently replace one clan's normalized policy via the admin RLS key."""

    if not isinstance(admin_key, str) or not admin_key.strip():
        raise PermissionError("Admin key is required.")
    normalized = validate_policy_payload(payload)
    try:
        configured_url, configured_key = _resolve_storage_config(
            supabase_url=supabase_url,
            api_key=api_key,
        )
        endpoint = f"{configured_url}/rest/v1/{POLICY_TABLE}?on_conflict=clan_tag"
        row = dict(normalized)
        json.dumps(row, ensure_ascii=False, separators=(",", ":"))
        response, attempts = _storage_request(
            "POST",
            endpoint,
            headers=_admin_headers(
                configured_key,
                admin_key.strip(),
                write=True,
                prefer="resolution=merge-duplicates,return=minimal",
            ),
            payload=row,
            timeout=timeout,
            max_retries=max_retries,
            backoff_factor=backoff_factor,
            max_backoff=max_backoff,
            sleep=sleep,
        )
        return _policy_result(
            str(normalized["clan_tag"]),
            normalized,
            status="stored",
            source="request",
            attempts=attempts,
            metadata={"action": "upserted"},
        )
    except ClanPolicyStorageError as error:
        raise error
    except (TypeError, ValueError, OverflowError, json.JSONDecodeError):
        raise ClanPolicyStorageError("invalid_payload") from None


def _decision_result(
    decision: Mapping[str, object],
    *,
    status: str = "stored",
    action: str = "accepted",
    attempts: Optional[int] = None,
) -> Dict[str, object]:
    clean = dict(decision)
    result: Dict[str, object] = {
        "ok": True,
        "status": status,
        "action": action,
        "clan_tag": clean["clan_tag"],
        "decision": clean,
    }
    # The route is admin-only; these top-level aliases make the audit fields
    # easy to consume without ever placing them on a public route.
    result.update(clean)
    if attempts is not None:
        result["attempts"] = attempts
    return result


def read_leader_decisions(
    clan_tag: object,
    admin_key: object,
    *,
    limit: object = DEFAULT_DECISION_LIMIT,
    supabase_url: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout: float = DEFAULT_STORAGE_TIMEOUT,
    max_retries: int = DEFAULT_STORAGE_MAX_RETRIES,
    backoff_factor: float = DEFAULT_STORAGE_BACKOFF_SECONDS,
    max_backoff: float = DEFAULT_STORAGE_MAX_BACKOFF_SECONDS,
    sleep: Optional[Callable[[float], None]] = None,
) -> Dict[str, object]:
    """Read only the requested clan's decisions through the admin boundary."""

    if not isinstance(admin_key, str) or not admin_key.strip():
        raise PermissionError("Admin key is required.")
    normalized_clan_tag = validate_clan_tag(clan_tag)
    decision_limit = _safe_integer(
        limit,
        "limit",
        minimum=1,
        maximum=MAX_DECISION_LIMIT,
    )
    try:
        configured_url, configured_key = _resolve_storage_config(
            supabase_url=supabase_url,
            api_key=api_key,
        )
        endpoint = f"{configured_url}/rest/v1/{LEADER_DECISIONS_TABLE}"
        response, attempts = _storage_request(
            "GET",
            endpoint,
            headers=_admin_headers(configured_key, admin_key.strip()),
            params={
                "select": (
                    "id,clan_tag,player_tag,actor,decision_type,reason,"
                    "related_race_key,created_at,idempotency_key"
                ),
                "clan_tag": f"eq.{normalized_clan_tag}",
                "order": "created_at.desc,id.desc",
                "limit": str(decision_limit),
            },
            timeout=timeout,
            max_retries=max_retries,
            backoff_factor=backoff_factor,
            max_backoff=max_backoff,
            sleep=sleep,
        )
        content = getattr(response, "content", b"")
        rows = response.json() if content else []
        if not isinstance(rows, list):
            raise ClanPolicyStorageError("invalid_response", attempts=attempts)
        decisions: List[Dict[str, object]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            normalized_row = _normalise_decision_row(row, normalized_clan_tag)
            if normalized_row is not None:
                decisions.append(normalized_row)
        return {
            "ok": True,
            "status": "stored" if decisions else "empty",
            "data_status": "stored" if decisions else "empty",
            "clan_tag": normalized_clan_tag,
            "decisions": decisions,
            "count": len(decisions),
            "limit": decision_limit,
            "attempts": attempts,
        }
    except ClanPolicyStorageError:
        raise
    except (TypeError, ValueError, OverflowError, json.JSONDecodeError):
        raise ClanPolicyStorageError("invalid_response") from None


def write_leader_decision(
    payload: Mapping[str, object],
    admin_key: object,
    *,
    supabase_url: Optional[str] = None,
    api_key: Optional[str] = None,
    clock: Optional[Callable[[], object]] = None,
    timeout: float = DEFAULT_STORAGE_TIMEOUT,
    max_retries: int = DEFAULT_STORAGE_MAX_RETRIES,
    backoff_factor: float = DEFAULT_STORAGE_BACKOFF_SECONDS,
    max_backoff: float = DEFAULT_STORAGE_MAX_BACKOFF_SECONDS,
    sleep: Optional[Callable[[float], None]] = None,
) -> Dict[str, object]:
    """Append one audit decision; retries use the durable natural key."""

    if not isinstance(admin_key, str) or not admin_key.strip():
        raise PermissionError("Admin key is required.")
    decision = validate_decision_payload(payload, clock=clock)
    # Avoid persisting the credential if a caller accidentally copied it into
    # an audit field.  `_safe_text` already checks configured environment
    # secrets; this also covers the request header itself.
    for field in ("actor", "reason", "related_race_key"):
        value = decision.get(field)
        if isinstance(value, str) and admin_key.strip() in value:
            raise ValueError(f"{field} is invalid.")
    try:
        configured_url, configured_key = _resolve_storage_config(
            supabase_url=supabase_url,
            api_key=api_key,
        )
        endpoint = (
            f"{configured_url}/rest/v1/{LEADER_DECISIONS_TABLE}"
            "?on_conflict=clan_tag,idempotency_key"
        )
        json.dumps(decision, ensure_ascii=False, separators=(",", ":"))
        response, attempts = _storage_request(
            "POST",
            endpoint,
            headers=_admin_headers(
                configured_key,
                admin_key.strip(),
                write=True,
                # Append-only plus a unique key means retrying the same
                # request cannot update the original audit record.
                prefer="resolution=ignore-duplicates,return=minimal",
            ),
            payload=decision,
            timeout=timeout,
            max_retries=max_retries,
            backoff_factor=backoff_factor,
            max_backoff=max_backoff,
            sleep=sleep,
        )
        del response  # The minimal response intentionally contains no data.
        return _decision_result(decision, attempts=attempts)
    except ClanPolicyStorageError:
        raise
    except (TypeError, ValueError, OverflowError, json.JSONDecodeError):
        raise ClanPolicyStorageError("invalid_payload") from None


def public_policy_payload(result: Mapping[str, object]) -> Dict[str, object]:
    """Return only the non-sensitive policy read model for public routes."""

    policy = result.get("policy")
    try:
        candidate_clan = result.get("clan_tag")
        if not candidate_clan and isinstance(policy, Mapping):
            candidate_clan = policy.get("clan_tag")
        clan_tag = validate_clan_tag(candidate_clan)
    except ValueError:
        clan_tag = ""
    clean_policy: Dict[str, object] = (
        _default_policy_for(clan_tag) if clan_tag else {"clan_tag": ""}
    )
    if isinstance(policy, Mapping) and clan_tag:
        candidate = {"clan_tag": clan_tag}
        for field in POLICY_FIELDS:
            if field in policy and policy[field] is not None:
                candidate[field] = policy[field]
        try:
            clean_policy = validate_policy_payload(candidate)
        except ValueError:
            clean_policy = _default_policy_for(clan_tag)
    for field in POLICY_FIELDS:
        clean_policy.setdefault(field, DEFAULT_CLAN_POLICY[field])
    return {
        "ok": bool(result.get("ok")),
        "status": result.get("status", "error"),
        "data_status": result.get("data_status", result.get("status", "error")),
        "clan_tag": clean_policy["clan_tag"],
        "policy_source": result.get("policy_source", result.get("source", "defaults")),
        "policy": clean_policy,
        **{field: clean_policy.get(field) for field in POLICY_FIELDS},
        **({"error": result["error"]} if result.get("error") else {}),
    }


__all__ = [
    "ADMIN_KEY_HEADER",
    "ANALYTICS_ADMIN_KEY_HEADER",
    "DECISION_TYPES",
    "DEFAULT_CLAN_POLICY",
    "DEFAULT_DECISION_LIMIT",
    "DEFAULT_POLICY",
    "LEADER_DECISIONS_TABLE",
    "MAX_DECISION_LIMIT",
    "POLICY_FIELDS",
    "POLICY_TABLE",
    "ClanPolicyStorageError",
    "get_clan_policy",
    "load_clan_policy",
    "public_policy_payload",
    "read_clan_policy",
    "read_leader_decisions",
    "validate_clan_tag",
    "validate_decision_payload",
    "validate_policy_payload",
    "write_clan_policy",
    "write_leader_decision",
]
