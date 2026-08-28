"""Read-only River Race status route.

This module is the public read model for one configured Brabant Royale clan.
It deliberately has no write, monitor or webhook dependency: current data is
read through the T01 client, interpretation is delegated to the T02
normalizers, previous observations use the T06 read seam, and Duel-first
classification is delegated to the T07 domain function.

The public response contains per-player status rows because those rows are
the route's primary read model.  Alert details are a separate trust boundary:
the default response only contains aggregate alert counts.  A caller must
explicitly request ``view=leader`` (or ``leader=1``) and pass the
server-configured ``X-War-Status-Leader-Secret`` before individual alert
rows are returned.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
import hmac
import json
import os
import re
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence
from urllib.parse import parse_qs, urlparse

try:
    from api.clash_client import ClashRoyaleClient, normalize_tag
    from api.clash_normalizers import (
        DATA_STATUS_EMPTY,
        DATA_STATUS_ERROR,
        DATA_STATUS_FRESH,
        DATA_STATUS_INVALID,
        DATA_STATUS_PARTIAL,
        DATA_STATUS_STALE,
        DATA_STATUS_UNKNOWN,
        normalize_clan,
        normalize_current_river_race,
        normalize_members,
    )
    from api.duel_first import (
        DuelFirstValidationError,
        STATUS_API_STALE,
        STATUS_DUEL_FIRST_LIKELY,
        STATUS_NOT_STARTED,
        STATUS_SOLO_START_OBSERVED,
        STATUS_UNKNOWN_START,
        build_race_day_key,
        observe_duel_first,
    )
except ImportError:  # pragma: no cover - convenient when deployed as loose files.
    from clash_client import ClashRoyaleClient, normalize_tag
    from clash_normalizers import (
        DATA_STATUS_EMPTY,
        DATA_STATUS_ERROR,
        DATA_STATUS_FRESH,
        DATA_STATUS_INVALID,
        DATA_STATUS_PARTIAL,
        DATA_STATUS_STALE,
        DATA_STATUS_UNKNOWN,
        normalize_clan,
        normalize_current_river_race,
        normalize_members,
    )
    from duel_first import (
        DuelFirstValidationError,
        STATUS_API_STALE,
        STATUS_DUEL_FIRST_LIKELY,
        STATUS_NOT_STARTED,
        STATUS_SOLO_START_OBSERVED,
        STATUS_UNKNOWN_START,
        build_race_day_key,
        observe_duel_first,
    )

try:
    from api.config import CLAN_CONFIGS, DEFAULT_CLAN_TAG
except ImportError:  # pragma: no cover - convenient for loose-file loading.
    from config import CLAN_CONFIGS, DEFAULT_CLAN_TAG

try:
    from supabase_history import read_previous_player_snapshot
except ImportError:  # pragma: no cover - convenient when deployed as a package.
    from ..supabase_history import read_previous_player_snapshot


HTTP_STATUS_BAD_REQUEST = 400
HTTP_STATUS_FORBIDDEN = 403
HTTP_STATUS_BAD_GATEWAY = 502
HTTP_STATUS_METHOD_NOT_ALLOWED = 405
HTTP_STATUS_OK = 200

WAR_STATUS_LEADER_SECRET_ENV = "WAR_STATUS_LEADER_SECRET"
WAR_STATUS_LEADER_HEADER = "X-War-Status-Leader-Secret"
WAR_STATUS_LEADER_VIEW = "leader"

# Descriptive aliases keep the auth seam easy to discover without accepting a
# second credential or silently reusing the monitor's stronger secret.
LEADER_SECRET_ENV = WAR_STATUS_LEADER_SECRET_ENV
LEADER_SECRET_HEADER = WAR_STATUS_LEADER_HEADER

DECKS_PER_PLAYER_PER_DAY = 4
PUBLIC_STATUS_LIVE = "live"
PUBLIC_STATUS_STALE = "stale"
PUBLIC_STATUS_ERROR = "error"
PUBLIC_STATUS_EMPTY = "empty"

_RAW_STATUS_VALUES = frozenset(
    {
        DATA_STATUS_FRESH,
        DATA_STATUS_STALE,
        DATA_STATUS_EMPTY,
        DATA_STATUS_PARTIAL,
        DATA_STATUS_INVALID,
        DATA_STATUS_ERROR,
        DATA_STATUS_UNKNOWN,
        "ok",
    }
)
_SAFE_ERROR_CODES = frozenset(
    {
        "authentication_error",
        "bad_request",
        "clash_client_error",
        "configuration_error",
        "empty_response",
        "endpoint_error",
        "forbidden",
        "invalid_json",
        "invalid_payload",
        "invalid_request",
        "invalid_response",
        "invalid_tag",
        "normalization_error",
        "not_found",
        "rate_limited",
        "server_key_required",
        "stale",
        "supabase_http_error",
        "timeout",
        "transport_error",
        "unexpected_status",
        "upstream_server_error",
        "upstream_error",
    }
)
_DUEL_STATUSES = (
    STATUS_NOT_STARTED,
    STATUS_SOLO_START_OBSERVED,
    STATUS_DUEL_FIRST_LIKELY,
    STATUS_UNKNOWN_START,
    STATUS_API_STALE,
)
_ALERT_STATUSES = frozenset(
    {STATUS_SOLO_START_OBSERVED, STATUS_DUEL_FIRST_LIKELY}
)
_SECRET_ENV_NAMES = (
    "CLASH_ROYALE_API_KEY",
    "SUPABASE_SECRET_KEY",
    "SUPABASE_SERVICE_ROLE_KEY",
    "SUPABASE_PUBLISHABLE_KEY",
    "SUPABASE_ANON_KEY",
    "SUPABASE_INGEST_TOKEN",
    "WAR_MONITOR_SECRET",
    WAR_STATUS_LEADER_SECRET_ENV,
)


class InvalidClanTagError(ValueError):
    """A requested clan tag is malformed or not repository-configured."""


class WarStatusConfigurationError(RuntimeError):
    """A safe configuration failure without upstream exception text."""


def _configured_secret_values() -> Iterable[str]:
    for env_name in _SECRET_ENV_NAMES:
        value = os.environ.get(env_name, "").strip()
        if value:
            yield value


def _safe_text(value: Any, default: Optional[str] = None, *, maximum: int = 160) -> Optional[str]:
    if not isinstance(value, str):
        return default
    result = value.strip()
    if not result or any(ord(character) < 32 or ord(character) == 127 for character in result):
        return default
    if any(secret in result for secret in _configured_secret_values()):
        return "[redacted]"
    return result[:maximum]


def _safe_int(value: Any, *, minimum: Optional[int] = None) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        try:
            result = int(value.strip())
        except (TypeError, ValueError, OverflowError):
            return None
    else:
        return None
    if minimum is not None and result < minimum:
        return None
    return result


def _safe_timestamp(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        parsed = None
        for pattern in ("%Y%m%dT%H%M%S.%fZ", "%Y%m%dT%H%M%SZ"):
            try:
                parsed = datetime.strptime(raw, pattern).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        if parsed is None:
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except (TypeError, ValueError, OverflowError):
                return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _now_timestamp(clock: Optional[Callable[[], Any]] = None) -> str:
    if clock is not None:
        try:
            value = _safe_timestamp(clock())
            if value is not None:
                return value
        except Exception:
            pass
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _value(source: Any, *keys: str, default: Any = None) -> Any:
    for key in keys:
        try:
            if isinstance(source, Mapping) and key in source:
                return source[key]
            if hasattr(source, key):
                return getattr(source, key)
        except Exception:
            continue
    return default


def _metadata(source: Any) -> Any:
    return _value(source, "metadata", default=None)


def _raw_data_status(source: Any, default: str = DATA_STATUS_UNKNOWN) -> str:
    candidates = (
        _value(source, "data_status", "dataStatus", "status", default=None),
        _value(_metadata(source), "data_status", "dataStatus", "status", default=None),
    )
    for candidate in candidates:
        if isinstance(candidate, str):
            normalized = candidate.strip().lower()
            if normalized in _RAW_STATUS_VALUES:
                return DATA_STATUS_FRESH if normalized == "ok" else normalized
    if _value(source, "is_stale", "stale", default=False) is True:
        return DATA_STATUS_STALE
    if _value(_metadata(source), "is_stale", "stale", default=False) is True:
        return DATA_STATUS_STALE
    if _value(_metadata(source), "empty", default=False) is True:
        return DATA_STATUS_EMPTY
    return default


def _public_status(raw_status: str) -> str:
    if raw_status == DATA_STATUS_STALE:
        return PUBLIC_STATUS_STALE
    if raw_status in {DATA_STATUS_ERROR, DATA_STATUS_INVALID}:
        return PUBLIC_STATUS_ERROR
    if raw_status == DATA_STATUS_EMPTY:
        return PUBLIC_STATUS_EMPTY
    # A T02 partial model is still a live observation.  The missing fields
    # remain explicit in data_quality; they are not turned into zeros.
    return PUBLIC_STATUS_LIVE


def _safe_error_code(error: Any, default: str = "upstream_error") -> str:
    candidate = (
        error
        if isinstance(error, str)
        else _value(error, "code", "error_code", "error", default=None)
    )
    if isinstance(candidate, str):
        candidate = candidate.strip().lower()
        if candidate in _SAFE_ERROR_CODES:
            return candidate
    return default


def _safe_reason(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    if candidate == "age" or candidate in _SAFE_ERROR_CODES:
        return candidate
    return None


def _freshness(source: Any, *, default_status: str = DATA_STATUS_UNKNOWN) -> Dict[str, Any]:
    raw_status = _raw_data_status(source, default_status)
    metadata = _metadata(source)
    stale = bool(
        _value(source, "is_stale", "stale", default=False) is True
        or _value(metadata, "is_stale", "stale", default=False) is True
        or raw_status == DATA_STATUS_STALE
    )
    source_value = _safe_text(
        _value(source, "source", default=None)
        or _value(metadata, "source", default=None),
        None,
        maximum=120,
    )
    result: Dict[str, Any] = {
        "status": _public_status(raw_status),
        "data_status": raw_status,
        "is_stale": stale,
        "stale": stale,
        "source": source_value,
        "fetched_at": _safe_timestamp(
            _value(source, "fetched_at", default=None)
            or _value(metadata, "fetched_at", default=None)
        ),
        "captured_at": _safe_timestamp(
            _value(source, "captured_at", default=None)
            or _value(metadata, "captured_at", default=None)
        ),
        "stale_reason": _safe_reason(
            _value(source, "stale_reason", default=None)
            or _value(metadata, "stale_reason", default=None)
        ),
        "error_code": _safe_error_code(source, default="")
        or _safe_error_code(metadata, default="")
        or None,
        "status_code": _safe_int(
            _value(source, "status_code", default=None)
            or _value(metadata, "status_code", default=None),
            minimum=100,
        ),
        "attempts": _safe_int(
            _value(source, "attempts", default=None)
            or _value(metadata, "attempts", default=None),
            minimum=0,
        ),
    }
    return result


def _error_freshness(error: Any) -> Dict[str, Any]:
    result = {
        "status": PUBLIC_STATUS_ERROR,
        "data_status": DATA_STATUS_ERROR,
        "is_stale": False,
        "stale": False,
        "source": None,
        "fetched_at": None,
        "captured_at": None,
        "stale_reason": None,
        "error_code": _safe_error_code(error),
        "status_code": _safe_int(_value(error, "status_code", default=None), minimum=100),
        "attempts": _safe_int(_value(error, "attempts", default=None), minimum=0),
    }
    return result


def _safe_tag(value: Any) -> Optional[str]:
    try:
        normalized = normalize_tag(value)
    except Exception:
        return None
    if any(secret == normalized for secret in _configured_secret_values()):
        return None
    return normalized


def _resolve_clan_config(
    clan_tag: Any,
    clan_configs: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Dict[str, str]:
    raw_tag = DEFAULT_CLAN_TAG if clan_tag is None or clan_tag == "" else clan_tag
    normalized = _safe_tag(raw_tag)
    if normalized is None:
        raise InvalidClanTagError("Invalid clan tag.")

    configured = CLAN_CONFIGS if clan_configs is None else clan_configs
    if not isinstance(configured, Mapping):
        raise WarStatusConfigurationError("Clan configuration is unavailable.")
    for config_key, supplied in configured.items():
        supplied_mapping = supplied if isinstance(supplied, Mapping) else {}
        candidate = _safe_tag(supplied_mapping.get("tag", config_key))
        if candidate != normalized:
            continue
        name = _safe_text(supplied_mapping.get("name"), normalized) or normalized
        return {"tag": normalized, "name": name}
    # Do not use the permissive legacy configuration helper here: it falls back
    # to the default clan for unknown tags, which is unsafe at an API boundary.
    raise InvalidClanTagError("Invalid clan tag.")


def validate_clan_tag(
    clan_tag: Any,
    clan_configs: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> str:
    """Return a canonical tag only when it is explicitly configured."""

    return _resolve_clan_config(clan_tag, clan_configs)["tag"]


def parse_clan_tag(path: str) -> Any:
    """Read the optional ``clan`` query parameter without normalizing it."""

    try:
        params = parse_qs(urlparse(path or "").query, keep_blank_values=True)
    except (TypeError, ValueError):
        raise InvalidClanTagError("Invalid clan tag.") from None
    values = params.get("clan")
    if not values or (len(values) == 1 and not values[0].strip()):
        return DEFAULT_CLAN_TAG
    if len(values) != 1:
        raise InvalidClanTagError("Invalid clan tag.")
    return values[0]


def leader_view_requested(path: str) -> bool:
    """Return whether the caller explicitly asked for the leader view."""

    try:
        params = parse_qs(urlparse(path or "").query, keep_blank_values=True)
    except (TypeError, ValueError):
        return False
    view_values = [
        *(params.get("view") or []),
        *(params.get("scope") or []),
    ]
    if any(isinstance(value, str) and value.strip().lower() == WAR_STATUS_LEADER_VIEW for value in view_values):
        return True
    return any(
        isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}
        for value in (params.get("leader") or [])
    )


def leader_secret_matches(provided: Any, expected: Optional[Any] = None) -> bool:
    """Compare the leader credential without returning or logging it."""

    configured = (
        os.environ.get(WAR_STATUS_LEADER_SECRET_ENV, "")
        if expected is None
        else expected
    )
    if not isinstance(provided, str) or not isinstance(configured, str):
        return False
    supplied_value = provided.strip()
    expected_value = configured.strip()
    if not supplied_value or not expected_value:
        return False
    return hmac.compare_digest(supplied_value, expected_value)


def leader_request_authorized(path: str, headers: Any) -> bool:
    """Require both an explicit leader request and the server-side secret."""

    if not leader_view_requested(path):
        return False
    supplied = ""
    if isinstance(headers, Mapping):
        for key, value in headers.items():
            if str(key).lower() == WAR_STATUS_LEADER_HEADER.lower():
                supplied = value or ""
                break
    else:
        try:
            supplied = headers.get(WAR_STATUS_LEADER_HEADER, "")
        except Exception:
            supplied = ""
    return leader_secret_matches(supplied)


def _resolve_read_previous_snapshot(
    storage: Any,
    override: Optional[Callable[..., Mapping[str, Any]]],
) -> Optional[Callable[..., Mapping[str, Any]]]:
    if override is not None:
        if not callable(override):
            raise WarStatusConfigurationError("Injected read function is not callable.")
        return override
    if storage is not None:
        candidate = (
            storage.get("read_previous_player_snapshot")
            if isinstance(storage, Mapping)
            else getattr(storage, "read_previous_player_snapshot", None)
        )
        if candidate is None:
            return None
        if not callable(candidate):
            raise WarStatusConfigurationError("Injected read function is not callable.")
        return candidate
    return read_previous_player_snapshot


def _resolve_client(client: Any, client_factory: Optional[Callable[[], Any]]) -> Any:
    if client is not None:
        return client
    try:
        if client_factory is not None:
            if not callable(client_factory):
                raise TypeError
            return client_factory()
        return ClashRoyaleClient()
    except Exception:
        raise WarStatusConfigurationError("Clash client is unavailable.") from None


def _fetch_endpoints(client: Any, clan_tag: str) -> tuple[Dict[str, Any], Dict[str, Dict[str, Any]], List[Dict[str, str]]]:
    responses: Dict[str, Any] = {}
    freshness: Dict[str, Dict[str, Any]] = {}
    errors: List[Dict[str, str]] = []
    for operation, method_name in (
        ("clan", "get_clan"),
        ("members", "get_members"),
        ("race", "get_current_river_race"),
    ):
        try:
            method = getattr(client, method_name)
            response = method(clan_tag)
            if response is None:
                raise WarStatusConfigurationError("Endpoint returned no response.")
            responses[operation] = response
            freshness[operation] = _freshness(
                response,
                default_status=DATA_STATUS_EMPTY,
            )
        except Exception as error:
            responses[operation] = None
            freshness[operation] = _error_freshness(error)
            errors.append({"operation": operation, "code": _safe_error_code(error)})
    freshness["current_river_race"] = dict(freshness["race"])
    return responses, freshness, errors


def _normalize_endpoints(
    responses: Mapping[str, Any],
    clan_tag: str,
) -> tuple[Any, Sequence[Any], Any, List[Dict[str, str]]]:
    normalized_clan: Any = None
    normalized_members: Sequence[Any] = ()
    normalized_race: Any = None
    errors: List[Dict[str, str]] = []

    if responses.get("clan") is not None:
        try:
            normalized_clan = normalize_clan(responses["clan"], clan_tag=clan_tag)
        except Exception:
            errors.append({"operation": "clan", "code": "normalization_error"})
    if responses.get("members") is not None:
        try:
            normalized_members = normalize_members(
                responses["members"],
                clan_tag=clan_tag,
            )
        except Exception:
            errors.append({"operation": "members", "code": "normalization_error"})
    if responses.get("race") is not None:
        try:
            normalized_race = normalize_current_river_race(
                responses["race"],
                clan_tag=clan_tag,
                members=normalized_members,
            )
        except Exception:
            errors.append({"operation": "race", "code": "normalization_error"})
    return normalized_clan, normalized_members, normalized_race, errors


def _overall_raw_status(
    responses: Mapping[str, Any],
    freshness: Mapping[str, Mapping[str, Any]],
    normalized_race: Any,
    errors: Sequence[Mapping[str, str]],
) -> str:
    if errors:
        return DATA_STATUS_ERROR
    race_status = _raw_data_status(normalized_race, _raw_data_status(responses.get("race"), DATA_STATUS_EMPTY))
    if race_status in {DATA_STATUS_ERROR, DATA_STATUS_INVALID}:
        return DATA_STATUS_ERROR
    if race_status == DATA_STATUS_EMPTY:
        return DATA_STATUS_EMPTY
    all_statuses = [
        _raw_data_status(normalized_race, DATA_STATUS_UNKNOWN),
        *(
            record.get("data_status", DATA_STATUS_UNKNOWN)
            for record in freshness.values()
        ),
    ]
    if any(status == DATA_STATUS_STALE for status in all_statuses):
        return DATA_STATUS_STALE
    if any(status in {DATA_STATUS_ERROR, DATA_STATUS_INVALID} for status in all_statuses):
        return DATA_STATUS_ERROR
    if any(status == DATA_STATUS_PARTIAL for status in all_statuses):
        return DATA_STATUS_PARTIAL
    return DATA_STATUS_FRESH


def _context_value(context: Any, *keys: str) -> Any:
    return _value(context, *keys, default=None)


def _build_race_context(
    normalized_race: Any,
    clan_tag: str,
    raw_status: str,
    route_observed_at: str,
) -> Dict[str, Any]:
    context = _value(normalized_race, "context", default=None)
    context_tag = _safe_tag(_context_value(context, "clan_tag", "clanTag")) or clan_tag
    context_observed = _safe_timestamp(
        _context_value(context, "captured_at", "capturedAt")
    )
    if context is None:
        context_observed = None
    source = _safe_text(
        _value(context, "source", default=None)
        or _value(normalized_race, "source", default=None),
        None,
        maximum=120,
    )
    fetched_at = _safe_timestamp(
        _value(context, "fetched_at", default=None)
        or _value(normalized_race, "fetched_at", default=None)
    )
    return {
        "clan_tag": context_tag,
        "season_id": _safe_int(_context_value(context, "season_id", "seasonId"), minimum=0),
        "section_index": _safe_int(_context_value(context, "section_index", "sectionIndex"), minimum=0),
        "period_index": _safe_int(_context_value(context, "period_index", "periodIndex"), minimum=0),
        "period_type": _safe_text(_context_value(context, "period_type", "periodType")),
        "state": _safe_text(_context_value(context, "state")),
        "race_created_at": _safe_timestamp(
            _context_value(context, "race_created_at", "raceCreatedAt")
        ),
        "observed_at": context_observed,
        "source": source,
        "fetched_at": fetched_at,
        "status": _public_status(raw_status),
        "data_status": raw_status,
        "is_stale": raw_status == DATA_STATUS_STALE
        or bool(_value(normalized_race, "is_stale", "stale", default=False)),
        "stale_reason": _safe_reason(
            _value(context, "stale_reason", default=None)
            or _value(normalized_race, "stale_reason", default=None)
        ),
        "error_code": _safe_error_code(normalized_race, default="") or None,
        "route_observed_at": route_observed_at,
    }


def _metric(value: Any) -> Optional[int]:
    return _safe_int(value, minimum=0)


def _build_clan_rows(normalized_race: Any, clan_tag: str, route_observed_at: str) -> List[Dict[str, Any]]:
    clans = _value(normalized_race, "clans", default=()) or ()
    rows: List[Dict[str, Any]] = []
    for clan in clans:
        current_tag = _safe_tag(_value(clan, "clan_tag", "tag", default=None))
        if current_tag is None:
            continue
        raw_status = _raw_data_status(clan, DATA_STATUS_UNKNOWN)
        rows.append(
            {
                "clan_tag": current_tag,
                "name": _safe_text(_value(clan, "name", default=None)),
                "is_opponent": _value(clan, "is_opponent", default=None)
                if isinstance(_value(clan, "is_opponent", default=None), bool)
                else current_tag != clan_tag,
                "member_count": _metric(_value(clan, "member_count", default=None)),
                "clan_type": _safe_text(_value(clan, "clan_type", default=None)),
                "rank": _metric(_value(clan, "rank", default=None)),
                "fame": _metric(_value(clan, "fame", default=None)),
                "repair_points": _metric(_value(clan, "repair_points", default=None)),
                "decks_used_today": _metric(
                    _value(clan, "decks_used_today", "decksUsedToday", default=None)
                ),
                "boat_attacks_today": _metric(
                    _value(clan, "boat_attacks_today", "boatAttacksToday", default=None)
                ),
                "boat_defenses_today": _metric(
                    _value(clan, "boat_defenses_today", "boatDefensesToday", default=None)
                ),
                "battles_played": _metric(_value(clan, "battles_played", default=None)),
                "wins": _metric(_value(clan, "wins", default=None)),
                "observed_at": _safe_timestamp(
                    _value(clan, "captured_at", default=None)
                )
                or route_observed_at,
                "source": _safe_text(_value(clan, "source", default=None), None, maximum=120),
                "status": _public_status(raw_status),
                "data_status": raw_status,
                "is_stale": bool(_value(clan, "is_stale", "stale", default=False)),
            }
        )
    rows.sort(
        key=lambda row: (
            row["clan_tag"] != clan_tag,
            row["rank"] if row["rank"] is not None else 10**9,
            (row["name"] or "").casefold(),
        )
    )
    return rows


def _previous_snapshot_data(result: Any) -> tuple[Optional[Mapping[str, Any]], str]:
    status = _raw_data_status(result, DATA_STATUS_EMPTY)
    if status == "ok":
        status = DATA_STATUS_FRESH
    snapshot = _value(result, "snapshot", default=None)
    if status not in {DATA_STATUS_FRESH, DATA_STATUS_PARTIAL} or not isinstance(snapshot, Mapping):
        return None, status
    return snapshot, status


def _unknown_classification(status: str, confidence: str = "low") -> Dict[str, Any]:
    return {
        "status": status,
        "confidence": confidence,
        "observed_at": None,
        "previous_decks_used_today": None,
        "current_decks_used_today": None,
    }


def _classify_player(
    *,
    clan_tag: str,
    context: Any,
    participant: Any,
    observed_at: Optional[str],
    race_day_key: Optional[str],
    read_previous_snapshot_fn: Optional[Callable[..., Mapping[str, Any]]],
    previous_results: List[Mapping[str, Any]],
    errors: List[Dict[str, str]],
    is_stale: bool,
) -> Dict[str, Any]:
    player_tag = _safe_tag(_value(participant, "player_tag", "tag", default=None))
    if player_tag is None:
        return _unknown_classification(STATUS_UNKNOWN_START)
    current = _metric(_value(participant, "decks_used_today", "decksUsedToday", default=None))
    if observed_at is None:
        result = _unknown_classification(
            STATUS_API_STALE if is_stale else STATUS_UNKNOWN_START,
            "unknown",
        )
        result["current_decks_used_today"] = current
        return result

    context_values = {
        "season_id": _context_value(context, "season_id", "seasonId"),
        "section_index": _context_value(context, "section_index", "sectionIndex"),
        "race_created_at": _context_value(context, "race_created_at", "raceCreatedAt"),
        "period_index": _context_value(context, "period_index", "periodIndex"),
    }
    if any(value is None for value in context_values.values()):
        result = _unknown_classification(
            STATUS_API_STALE if is_stale else STATUS_UNKNOWN_START,
            "unknown",
        )
        result["observed_at"] = observed_at
        result["current_decks_used_today"] = current
        return result

    previous_snapshot: Optional[Mapping[str, Any]] = None
    previous_status = DATA_STATUS_EMPTY
    if not is_stale and current is not None and read_previous_snapshot_fn is not None:
        try:
            previous_result = read_previous_snapshot_fn(
                clan_tag,
                context_values["race_created_at"],
                context_values["period_index"],
                player_tag,
                before_captured_at=observed_at,
            )
            previous_results.append(
                {
                    "status": _public_status(_raw_data_status(previous_result, DATA_STATUS_EMPTY)),
                    "data_status": _raw_data_status(previous_result, DATA_STATUS_EMPTY),
                }
            )
            previous_snapshot, previous_status = _previous_snapshot_data(previous_result)
            if previous_status in {DATA_STATUS_ERROR, DATA_STATUS_INVALID, DATA_STATUS_STALE}:
                errors.append({"operation": "previous_snapshot", "code": _safe_error_code(previous_result)})
        except Exception as error:
            previous_results.append(_error_freshness(error))
            errors.append({"operation": "previous_snapshot", "code": _safe_error_code(error)})
            previous_snapshot = None
            previous_status = DATA_STATUS_ERROR

    previous_count = _metric(
        _value(previous_snapshot, "decks_used_today", "decksUsedToday", default=None)
    )
    try:
        classification = observe_duel_first(
            clan_tag=clan_tag,
            season_id=context_values["season_id"],
            section_index=context_values["section_index"],
            race_created_at=context_values["race_created_at"],
            period_index=context_values["period_index"],
            player_tag=player_tag,
            current_decks_used_today=current,
            previous_decks_used_today=previous_count,
            previous_race_day_key=race_day_key if previous_snapshot is not None else None,
            observed_at=observed_at,
            api_stale=is_stale,
        )
    except DuelFirstValidationError:
        errors.append({"operation": "duel_first", "code": "invalid_request"})
        result = _unknown_classification(
            STATUS_API_STALE if is_stale else STATUS_UNKNOWN_START,
            "unknown",
        )
        result["observed_at"] = observed_at
        result["current_decks_used_today"] = current
        result["previous_decks_used_today"] = previous_count
        return result
    except Exception:
        errors.append({"operation": "duel_first", "code": "normalization_error"})
        result = _unknown_classification(
            STATUS_API_STALE if is_stale else STATUS_UNKNOWN_START,
            "unknown",
        )
        result["observed_at"] = observed_at
        result["current_decks_used_today"] = current
        result["previous_decks_used_today"] = previous_count
        return result

    return {
        "status": classification.status,
        "confidence": classification.confidence,
        "observed_at": classification.observed_at,
        "previous_decks_used_today": classification.previous_decks_used_today,
        "current_decks_used_today": classification.current_decks_used_today,
    }


def _remaining_decks(used: Optional[int]) -> Optional[int]:
    if used is None or used < 0 or used > DECKS_PER_PLAYER_PER_DAY:
        return None
    return DECKS_PER_PLAYER_PER_DAY - used


def _build_player_rows(
    normalized_race: Any,
    normalized_members: Sequence[Any],
    clan_tag: str,
    route_observed_at: str,
    read_previous_snapshot_fn: Optional[Callable[..., Mapping[str, Any]]],
    previous_results: List[Mapping[str, Any]],
    errors: List[Dict[str, str]],
    raw_status: str,
    leader_verified: bool,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    participants = list(_value(normalized_race, "participants", default=()) or ())
    if not participants and raw_status in {DATA_STATUS_EMPTY, DATA_STATUS_ERROR}:
        return [], []
    participant_by_tag = {
        _safe_tag(_value(participant, "player_tag", "tag", default=None)): participant
        for participant in participants
        if _safe_tag(_value(participant, "player_tag", "tag", default=None)) is not None
    }
    member_by_tag = {
        _safe_tag(_value(member, "player_tag", "tag", default=None)): member
        for member in normalized_members
        if _safe_tag(_value(member, "player_tag", "tag", default=None)) is not None
    }
    ordered_tags: List[str] = []
    for member in normalized_members:
        tag = _safe_tag(_value(member, "player_tag", "tag", default=None))
        if tag is not None and tag not in ordered_tags:
            ordered_tags.append(tag)
    for participant in participants:
        tag = _safe_tag(_value(participant, "player_tag", "tag", default=None))
        if tag is not None and tag not in ordered_tags:
            ordered_tags.append(tag)

    context = _value(normalized_race, "context", default=None)
    race_day_key: Optional[str] = None
    try:
        race_day_key = build_race_day_key(
            clan_tag,
            _context_value(context, "season_id", "seasonId"),
            _context_value(context, "section_index", "sectionIndex"),
            _context_value(context, "race_created_at", "raceCreatedAt"),
            _context_value(context, "period_index", "periodIndex"),
        )
    except DuelFirstValidationError:
        if participants:
            errors.append({"operation": "race_identity", "code": "invalid_request"})

    rows: List[Dict[str, Any]] = []
    classifications: List[Dict[str, Any]] = []
    for tag in ordered_tags:
        participant = participant_by_tag.get(tag)
        member = member_by_tag.get(tag)
        name = _safe_text(
            _value(participant, "name", default=None)
            or _value(member, "name", default=None)
        )
        role = _safe_text(
            _value(participant, "role", default=None)
            or _value(member, "role", default=None)
        )
        participant_observed_at = _safe_timestamp(
            _value(participant, "captured_at", default=None)
        )
        if participant is not None:
            participant_observed_at = (
                participant_observed_at
                or _safe_timestamp(_value(context, "captured_at", default=None))
                or route_observed_at
            )
            classification = _classify_player(
                clan_tag=clan_tag,
                context=context,
                participant=participant,
                observed_at=participant_observed_at,
                race_day_key=race_day_key,
                read_previous_snapshot_fn=read_previous_snapshot_fn,
                previous_results=previous_results,
                errors=errors,
                is_stale=raw_status in {DATA_STATUS_STALE, DATA_STATUS_ERROR}
                or bool(_value(participant, "is_stale", "stale", default=False)),
            )
        else:
            classification = _unknown_classification(STATUS_UNKNOWN_START)
        current = _metric(
            _value(participant, "decks_used_today", "decksUsedToday", default=None)
        )
        if participant is None:
            classification["current_decks_used_today"] = None
        row = {
            "player_tag": tag,
            "name": name,
            "role": role,
            "decks_used_today": current,
            "decks_remaining_today": _remaining_decks(current),
            # A public player row remains useful for action tracking, but an
            # alertable Duel-first classification is an individual violation
            # signal and therefore stays leader-only.  Keep the field in the
            # stable response contract with an explicit redaction marker;
            # aggregate counts below remain public.
            "duel_first_status": (
                classification["status"]
                if leader_verified or classification["status"] not in _ALERT_STATUSES
                else "redacted"
            ),
            "status_confidence": (
                classification["confidence"]
                if leader_verified or classification["status"] not in _ALERT_STATUSES
                else "redacted"
            ),
            "observed_at": participant_observed_at,
        }
        rows.append(row)
        classifications.append(
            {
                "player_tag": tag,
                "name": name,
                "role": role,
                "status": classification["status"],
                "confidence": classification["confidence"],
                "observed_at": participant_observed_at,
                "missing_metrics": [
                    field
                    for field, value in (
                        ("decks_used_today", current),
                        ("decks_remaining_today", row["decks_remaining_today"]),
                    )
                    if value is None
                ],
            }
        )
    return rows, classifications


def _duel_first_summary(
    classifications: Sequence[Mapping[str, Any]],
    raw_status: str,
) -> Dict[str, Any]:
    counts = Counter(
        str(item.get("status"))
        for item in classifications
        if item.get("status") in _DUEL_STATUSES
    )
    status_counts = {status: counts.get(status, 0) for status in _DUEL_STATUSES}
    confidence_counts = Counter(
        str(item.get("confidence"))
        for item in classifications
        if item.get("confidence")
    )
    return {
        "status": _public_status(raw_status),
        "data_status": raw_status,
        "counts": status_counts,
        "status_counts": dict(status_counts),
        "confidence_counts": dict(confidence_counts),
        "players_observed": sum(
            1 for item in classifications if item.get("observed_at") is not None
        ),
        "players_classified": len(classifications),
        "alertable_count": sum(
            status_counts.get(status, 0) for status in _ALERT_STATUSES
        ),
    }


def _build_alerts(
    classifications: Sequence[Mapping[str, Any]],
    raw_status: str,
    *,
    leader_verified: bool,
) -> List[Dict[str, Any]]:
    if leader_verified:
        alerts: List[Dict[str, Any]] = []
        for item in classifications:
            if item.get("status") in _ALERT_STATUSES:
                alerts.append(
                    {
                        "type": "duel_first",
                        "scope": "leader",
                        "player_tag": item.get("player_tag"),
                        "name": item.get("name"),
                        "role": item.get("role"),
                        "status": item.get("status"),
                        "confidence": item.get("confidence"),
                        "observed_at": item.get("observed_at"),
                    }
                )
        return alerts

    counts = Counter(
        str(item.get("status"))
        for item in classifications
        if item.get("status") in _ALERT_STATUSES
    )
    alerts = [
        {
            "type": "duel_first",
            "scope": "public_aggregate",
            "status": status,
            "count": counts[status],
        }
        for status in (STATUS_SOLO_START_OBSERVED, STATUS_DUEL_FIRST_LIKELY)
        if counts[status]
    ]
    missing_count = sum(
        1 for item in classifications if item.get("missing_metrics")
    )
    if missing_count:
        alerts.append(
            {
                "type": "data_quality",
                "scope": "public_aggregate",
                "status": "missing_metrics",
                "count": missing_count,
            }
        )
    if raw_status in {DATA_STATUS_STALE, DATA_STATUS_ERROR, DATA_STATUS_EMPTY}:
        alerts.append(
            {
                "type": "data_freshness",
                "scope": "public_aggregate",
                "status": _public_status(raw_status),
                "count": 1,
            }
        )
    return alerts


def _data_quality(
    raw_status: str,
    normalized_clan: Any,
    normalized_members: Sequence[Any],
    normalized_race: Any,
    player_rows: Sequence[Mapping[str, Any]],
    errors: Sequence[Mapping[str, str]],
    freshness: Mapping[str, Mapping[str, Any]],
) -> Dict[str, Any]:
    missing: set[str] = set()
    estimated: set[str] = set()
    if normalized_clan is None or _safe_tag(_value(normalized_clan, "clan_tag", "tag", default=None)) is None:
        missing.add("clan")
    if not normalized_members:
        missing.add("members")
    if normalized_race is None:
        missing.add("race")
        missing.add("race_context")
    else:
        context = _value(normalized_race, "context", default=None)
        for field, keys in (
            ("season_id", ("season_id", "seasonId")),
            ("section_index", ("section_index", "sectionIndex")),
            ("period_index", ("period_index", "periodIndex")),
            ("race_created_at", ("race_created_at", "raceCreatedAt")),
        ):
            if _value(context, *keys, default=None) is None:
                missing.add(f"race_context.{field}")
        if not (_value(normalized_race, "clans", default=()) or ()):
            missing.add("race.clans")
        if not (_value(normalized_race, "participants", default=()) or ()):
            missing.add("race.participants")
    for field in ("decks_used_today", "decks_remaining_today"):
        if any(row.get(field) is None for row in player_rows):
            missing.add(field)
    if raw_status == DATA_STATUS_PARTIAL:
        estimated.add("decks_remaining_today")
    status = _public_status(raw_status)
    if status == PUBLIC_STATUS_LIVE and not missing and not errors:
        confidence = "high"
    elif status == PUBLIC_STATUS_STALE:
        confidence = "medium"
    else:
        confidence = "low" if status in {PUBLIC_STATUS_ERROR, PUBLIC_STATUS_EMPTY} else "medium"
    return {
        "status": status,
        "data_status": raw_status,
        "confidence": confidence,
        "missing_fields": sorted(missing),
        "missingFields": sorted(missing),
        "estimated_fields": sorted(estimated),
        "estimatedFields": sorted(estimated),
        "errors": [
            {
                "operation": _safe_text(error.get("operation"), "unknown"),
                "code": error.get("code")
                if error.get("code") in _SAFE_ERROR_CODES
                else "upstream_error",
            }
            for error in errors
        ],
        "sources": ["royaleapi_proxy", "supabase_read"],
        "normalizers": [
            "RaceContext",
            "RaceClan",
            "RaceParticipant",
            "PlayerProfile",
        ],
        "read_only": True,
        "alerts": "aggregate_public_or_leader_verified_individual",
        "metric_sources": {
            "decks_used_today": "official currentriverrace participant.decksUsedToday",
            "decks_remaining_today": "derived from official decksUsedToday and four-deck daily capacity",
            "duel_first_status": "T07 observe_duel_first",
        },
        "freshness_operations": sorted(freshness),
    }


def _empty_contract(
    *,
    clan_tag: Optional[str],
    clan_name: Optional[str],
    raw_status: str,
    error: Optional[str] = None,
    error_code: Optional[str] = None,
    http_status: int = HTTP_STATUS_BAD_GATEWAY,
) -> Dict[str, Any]:
    public_status = _public_status(raw_status)
    context = {
        "clan_tag": clan_tag,
        "season_id": None,
        "section_index": None,
        "period_index": None,
        "period_type": None,
        "state": None,
        "race_created_at": None,
        "observed_at": None,
        "source": None,
        "fetched_at": None,
        "status": public_status,
        "data_status": raw_status,
        "is_stale": raw_status == DATA_STATUS_STALE,
        "stale_reason": None,
        "error_code": error_code,
        "route_observed_at": None,
    }
    freshness = {
        "status": public_status,
        "data_status": raw_status,
        "is_stale": raw_status == DATA_STATUS_STALE,
        "stale": raw_status == DATA_STATUS_STALE,
        "clan": _error_freshness(error_code or "upstream_error"),
        "members": _error_freshness(error_code or "upstream_error"),
        "race": _error_freshness(error_code or "upstream_error"),
        "current_river_race": _error_freshness(error_code or "upstream_error"),
        "previous_snapshots": {
            "status": PUBLIC_STATUS_EMPTY,
            "data_status": DATA_STATUS_EMPTY,
            "observations": 0,
        },
    }
    quality = {
        "status": public_status,
        "data_status": raw_status,
        "confidence": "low",
        "missing_fields": ["clan", "members", "race", "race_context"],
        "missingFields": ["clan", "members", "race", "race_context"],
        "estimated_fields": [],
        "estimatedFields": [],
        "errors": ([{"operation": "route", "code": error_code}] if error_code else []),
        "sources": ["royaleapi_proxy", "supabase_read"],
        "normalizers": ["RaceContext", "RaceClan", "RaceParticipant", "PlayerProfile"],
        "read_only": True,
        "alerts": "aggregate_public_or_leader_verified_individual",
        "metric_sources": {},
        "freshness_operations": ["clan", "current_river_race", "members", "race"],
    }
    return {
        "ok": public_status != PUBLIC_STATUS_ERROR,
        "http_status": http_status,
        "status": public_status,
        "data_status": raw_status,
        "error": error,
        "generated_at": _now_timestamp(),
        "clan_tag": clan_tag,
        "clan_name": clan_name,
        "race_context": context,
        "clan_rows": [],
        "player_rows": [],
        "duel_first_summary": {
            "status": public_status,
            "data_status": raw_status,
            "counts": {status: 0 for status in _DUEL_STATUSES},
            "status_counts": {status: 0 for status in _DUEL_STATUSES},
            "confidence_counts": {},
            "players_observed": 0,
            "players_classified": 0,
            "alertable_count": 0,
        },
        "alerts": [],
        "alerts_scope": "public_aggregate",
        "leader_view": False,
        "freshness": freshness,
        "data_quality": quality,
    }


def build_war_status_payload(
    clan_tag: Any = None,
    *,
    client: Any = None,
    client_factory: Optional[Callable[[], Any]] = None,
    storage: Any = None,
    clan_configs: Optional[Mapping[str, Mapping[str, Any]]] = None,
    clock: Optional[Callable[[], Any]] = None,
    observed_at: Any = None,
    read_previous_snapshot_fn: Optional[Callable[..., Mapping[str, Any]]] = None,
    leader_verified: bool = False,
) -> Dict[str, Any]:
    """Build one safe War Status response using injectable read seams."""

    if not isinstance(leader_verified, bool):
        raise WarStatusConfigurationError("Leader verification must be boolean.")
    config = _resolve_clan_config(clan_tag, clan_configs)
    tag = config["tag"]
    route_observed_at = _safe_timestamp(observed_at) or _now_timestamp(clock)
    try:
        selected_client = _resolve_client(client, client_factory)
    except WarStatusConfigurationError:
        return _empty_contract(
            clan_tag=tag,
            clan_name=config["name"],
            raw_status=DATA_STATUS_ERROR,
            error="War status data is temporarily unavailable.",
            error_code="configuration_error",
        )

    try:
        read_previous = _resolve_read_previous_snapshot(storage, read_previous_snapshot_fn)
    except WarStatusConfigurationError:
        return _empty_contract(
            clan_tag=tag,
            clan_name=config["name"],
            raw_status=DATA_STATUS_ERROR,
            error="War status data is temporarily unavailable.",
            error_code="configuration_error",
        )

    responses, freshness, errors = _fetch_endpoints(selected_client, tag)
    normalized_clan, normalized_members, normalized_race, normalization_errors = _normalize_endpoints(
        responses,
        tag,
    )
    errors.extend(normalization_errors)
    raw_status = _overall_raw_status(
        responses,
        freshness,
        normalized_race,
        errors,
    )
    if normalized_race is not None:
        race_freshness = _freshness(normalized_race, default_status=DATA_STATUS_EMPTY)
        freshness["race"] = race_freshness
        freshness["current_river_race"] = dict(race_freshness)

    previous_results: List[Mapping[str, Any]] = []
    player_errors: List[Dict[str, str]] = []
    race_context = _build_race_context(
        normalized_race,
        tag,
        raw_status,
        route_observed_at,
    )
    clan_rows = _build_clan_rows(normalized_race, tag, route_observed_at)
    player_rows, classifications = _build_player_rows(
        normalized_race,
        normalized_members,
        tag,
        route_observed_at,
        read_previous,
        previous_results,
        player_errors,
        raw_status,
        bool(leader_verified),
    )
    errors.extend(player_errors)

    previous_statuses = [
        result.get("data_status", DATA_STATUS_UNKNOWN)
        for result in previous_results
    ]
    if not previous_statuses:
        previous_freshness: Dict[str, Any] = {
            "status": PUBLIC_STATUS_EMPTY,
            "data_status": DATA_STATUS_EMPTY,
            "is_stale": False,
            "stale": False,
            "observations": 0,
        }
    elif DATA_STATUS_ERROR in previous_statuses or DATA_STATUS_INVALID in previous_statuses:
        previous_freshness = {
            "status": PUBLIC_STATUS_ERROR,
            "data_status": DATA_STATUS_ERROR,
            "is_stale": False,
            "stale": False,
            "observations": len(previous_statuses),
        }
    elif DATA_STATUS_STALE in previous_statuses:
        previous_freshness = {
            "status": PUBLIC_STATUS_STALE,
            "data_status": DATA_STATUS_STALE,
            "is_stale": True,
            "stale": True,
            "observations": len(previous_statuses),
        }
    else:
        previous_freshness = {
            "status": PUBLIC_STATUS_LIVE,
            "data_status": DATA_STATUS_FRESH,
            "is_stale": False,
            "stale": False,
            "observations": len(previous_statuses),
        }
    freshness["previous_snapshots"] = previous_freshness

    # A failed optional history read must not turn a valid current API view
    # into fabricated zeroes.  It is recorded in data_quality while current
    # status remains live/stale/error based on current endpoints.
    quality = _data_quality(
        raw_status,
        normalized_clan,
        normalized_members,
        normalized_race,
        player_rows,
        errors,
        freshness,
    )
    summary = _duel_first_summary(classifications, raw_status)
    alerts = _build_alerts(
        classifications,
        raw_status,
        leader_verified=bool(leader_verified),
    )
    public_status = _public_status(raw_status)
    clan_name = _safe_text(
        _value(normalized_clan, "name", default=None),
        config["name"],
    ) or config["name"]
    return {
        "ok": public_status != PUBLIC_STATUS_ERROR,
        "http_status": HTTP_STATUS_OK if public_status != PUBLIC_STATUS_ERROR else HTTP_STATUS_BAD_GATEWAY,
        "status": public_status,
        "data_status": raw_status,
        "error": "War status data is temporarily unavailable."
        if public_status == PUBLIC_STATUS_ERROR
        else None,
        "generated_at": _now_timestamp(),
        "clan_tag": tag,
        "clan_name": clan_name,
        "race_context": race_context,
        "clan_rows": clan_rows,
        "player_rows": player_rows,
        "duel_first_summary": summary,
        "alerts": alerts,
        "alerts_scope": "leader" if leader_verified else "public_aggregate",
        "leader_view": bool(leader_verified),
        "freshness": freshness,
        "data_quality": quality,
    }


# Descriptive aliases keep the route's pure builder easy to find for callers
# and tests while retaining one implementation and one security boundary.
build_war_status = build_war_status_payload
collect_war_status = build_war_status_payload
get_war_status = build_war_status_payload


def _failure_payload(
    *,
    status: int,
    error: str,
    raw_status: str = DATA_STATUS_ERROR,
    error_code: str = "upstream_error",
) -> Dict[str, Any]:
    payload = _empty_contract(
        clan_tag=None,
        clan_name=None,
        raw_status=raw_status,
        error=error,
        error_code=error_code,
        http_status=status,
    )
    payload["ok"] = False
    payload["error"] = error
    return payload


class handler(BaseHTTPRequestHandler):
    """Vercel-compatible read-only ``GET /api/war_status`` handler."""

    def _send_json(self, status_code: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = getattr(self, "path", "")
        try:
            requested_tag = parse_clan_tag(path)
            config = _resolve_clan_config(requested_tag)
            leader_requested = leader_view_requested(path)
            authorized = leader_request_authorized(path, getattr(self, "headers", {}))
            if leader_requested and not authorized:
                self._send_json(
                    HTTP_STATUS_FORBIDDEN,
                    _failure_payload(
                        status=HTTP_STATUS_FORBIDDEN,
                        error="Unauthorized.",
                        error_code="forbidden",
                    ),
                )
                return
            payload = build_war_status_payload(
                config["tag"],
                leader_verified=authorized,
            )
            response_status = _safe_int(payload.get("http_status"), minimum=100) or HTTP_STATUS_OK
            self._send_json(response_status, payload)
        except InvalidClanTagError:
            self._send_json(
                HTTP_STATUS_BAD_REQUEST,
                _failure_payload(
                    status=HTTP_STATUS_BAD_REQUEST,
                    error="Invalid clan tag.",
                    error_code="invalid_tag",
                ),
            )
        except Exception:
            # Never serialize exception text: request libraries and injected
            # test transports can accidentally include API/database secrets.
            self._send_json(
                HTTP_STATUS_BAD_GATEWAY,
                _failure_payload(
                    status=HTTP_STATUS_BAD_GATEWAY,
                    error="War status data is temporarily unavailable.",
                ),
            )

    def do_POST(self) -> None:
        self._send_json(
            HTTP_STATUS_METHOD_NOT_ALLOWED,
            _failure_payload(
                status=HTTP_STATUS_METHOD_NOT_ALLOWED,
                error="Method not allowed.",
                raw_status=DATA_STATUS_ERROR,
                error_code="invalid_request",
            ),
        )

    def do_PUT(self) -> None:
        self.do_POST()

    def do_DELETE(self) -> None:
        self.do_POST()


__all__ = [
    "DECKS_PER_PLAYER_PER_DAY",
    "HTTP_STATUS_BAD_GATEWAY",
    "HTTP_STATUS_BAD_REQUEST",
    "HTTP_STATUS_FORBIDDEN",
    "HTTP_STATUS_METHOD_NOT_ALLOWED",
    "LEADER_SECRET_ENV",
    "LEADER_SECRET_HEADER",
    "PUBLIC_STATUS_EMPTY",
    "PUBLIC_STATUS_ERROR",
    "PUBLIC_STATUS_LIVE",
    "PUBLIC_STATUS_STALE",
    "WAR_STATUS_LEADER_HEADER",
    "WAR_STATUS_LEADER_SECRET_ENV",
    "WAR_STATUS_LEADER_VIEW",
    "InvalidClanTagError",
    "WarStatusConfigurationError",
    "build_war_status",
    "build_war_status_payload",
    "collect_war_status",
    "get_war_status",
    "handler",
    "leader_request_authorized",
    "leader_secret_matches",
    "leader_view_requested",
    "parse_clan_tag",
    "validate_clan_tag",
]
