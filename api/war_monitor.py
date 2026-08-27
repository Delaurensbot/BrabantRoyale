"""Authenticated, scheduled River Race monitor for the three configured clans.

The route is deliberately a thin HTTP adapter around :func:`run_war_monitor`.
It checks ``WAR_MONITOR_SECRET`` before constructing a client or touching
Supabase.  The request header is ``X-War-Monitor-Secret``; no compatibility
alias is accepted for the existing ``SUPABASE_INGEST_TOKEN`` because that
token protects a different route.

There is currently no notification policy in the repository.  Consequently,
notification queueing is disabled by default.  An operator may explicitly
opt in to queue-only records with ``WAR_MONITOR_NOTIFICATION_POLICY=pending``
or by supplying an injected policy to :func:`run_war_monitor`.  This module
never sends a Discord webhook.

All upstream reads use the T01 client and all payload interpretation uses the
T02 normalizers.  All raw monitoring I/O goes through the T06 Supabase
functions.  Missing counters stay ``None``; they are never converted to zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hmac
from http.server import BaseHTTPRequestHandler
import json
import os
import re
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    from api.clash_client import ClashRoyaleClient, normalize_tag
    from api.clash_normalizers import (
        DATA_STATUS_EMPTY,
        DATA_STATUS_ERROR,
        DATA_STATUS_INVALID,
        DATA_STATUS_PARTIAL,
        DATA_STATUS_STALE,
        normalize_clan,
        normalize_current_river_race,
        normalize_members,
    )
    from api.duel_first import (
        DuelFirstValidationError,
        build_race_day_key,
        observe_duel_first,
    )
except ImportError:  # pragma: no cover - useful when run as a loose Vercel file.
    from clash_client import ClashRoyaleClient, normalize_tag
    from clash_normalizers import (
        DATA_STATUS_EMPTY,
        DATA_STATUS_ERROR,
        DATA_STATUS_INVALID,
        DATA_STATUS_PARTIAL,
        DATA_STATUS_STALE,
        normalize_clan,
        normalize_current_river_race,
        normalize_members,
    )
    from duel_first import (
        DuelFirstValidationError,
        build_race_day_key,
        observe_duel_first,
    )

try:
    from Royale_api import CLAN_CONFIGS, get_clan_config
except ImportError:  # pragma: no cover - convenient for direct module loading.
    from ..Royale_api import CLAN_CONFIGS, get_clan_config

try:
    from supabase_history import (
        read_day_event,
        read_previous_player_snapshot,
        write_day_events,
        write_live_snapshots,
        write_notification_logs,
    )
except ImportError:  # pragma: no cover - convenient for package-style loading.
    from ..supabase_history import (
        read_day_event,
        read_previous_player_snapshot,
        write_day_events,
        write_live_snapshots,
        write_notification_logs,
    )


MONITOR_SECRET_ENV = "WAR_MONITOR_SECRET"
MONITOR_SECRET_HEADER = "X-War-Monitor-Secret"
NOTIFICATION_POLICY_ENV = "WAR_MONITOR_NOTIFICATION_POLICY"
NOTIFICATION_CHANNEL = "discord"
HTTP_STATUS_PARTIAL = 207

_ACTIVE_RACE_STATES = frozenset(
    {
        "active",
        "collectionday",
        "matchmaking",
        "warday",
    }
)
# Colosseum is still a live River Race period and must be snapshotted.  The
# phase-specific rules (for example, suppressing boat advice) belong to the
# strategy layer.  Training is the only period that is intentionally excluded
# from Duel-first monitoring.
_INACTIVE_PERIOD_TYPES = frozenset({"training"})
_KNOWN_STATUS_VALUES = frozenset(
    {
        "fresh",
        "stale",
        "empty",
        "partial",
        "invalid",
        "error",
        "unknown",
        "ok",
    }
)
_KNOWN_ERROR_CODES = frozenset(
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
        "monitor_error",
        "not_found",
        "normalization_error",
        "rate_limited",
        "server_key_required",
        "stale",
        "supabase_http_error",
        "timeout",
        "transport_error",
        "unexpected_status",
        "upstream_server_error",
    }
)


class MonitorConfigurationError(RuntimeError):
    """A safe, non-secret monitor configuration failure."""


@dataclass(frozen=True)
class MonitorClanConfig:
    """Small immutable copy of one repository clan configuration."""

    tag: str
    name: str


@dataclass(frozen=True)
class MonitorFunctions:
    """I/O seams used by the pure-ish orchestration layer and its tests."""

    write_live_snapshots: Callable[..., Mapping[str, Any]]
    read_previous_player_snapshot: Callable[..., Mapping[str, Any]]
    read_day_event: Optional[Callable[..., Mapping[str, Any]]]
    write_day_events: Callable[..., Mapping[str, Any]]
    write_notification_logs: Optional[Callable[..., Mapping[str, Any]]]


def configured_monitor_secret() -> Optional[str]:
    """Return the configured monitor secret without exposing it to callers."""

    value = os.environ.get(MONITOR_SECRET_ENV, "").strip()
    return value or None


def secret_matches(provided: object, expected: Optional[object] = None) -> bool:
    """Compare a request secret in constant time, with no secret in errors."""

    configured = configured_monitor_secret() if expected is None else expected
    if not isinstance(provided, str) or not isinstance(configured, str):
        return False
    supplied_value = provided.strip()
    expected_value = configured.strip()
    if not supplied_value or not expected_value:
        return False
    return hmac.compare_digest(supplied_value, expected_value)


def _utc_timestamp(value: Any = None) -> str:
    if value is None:
        parsed = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            raise MonitorConfigurationError("Monitor clock returned an invalid timestamp.") from None
    else:
        raise MonitorConfigurationError("Monitor clock returned an invalid timestamp.")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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


def _safe_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _safe_bool(value: Any, default: bool = False) -> bool:
    return value if isinstance(value, bool) else default


def _safe_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        try:
            return int(value.strip())
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def _safe_status(value: Any, default: str = "unknown") -> str:
    status = _safe_text(value).lower()
    return status if status in _KNOWN_STATUS_VALUES else default


def _safe_error_code(error: Any) -> str:
    code = _safe_text(getattr(error, "code", ""), "")
    return code if code in _KNOWN_ERROR_CODES else "monitor_error"


def _metadata(source: Any) -> Any:
    return _value(source, "metadata", default=None)


def _data_status(source: Any, default: str = "unknown") -> str:
    status = _value(source, "data_status", "status", default=None)
    if status is None:
        status = _value(_metadata(source), "data_status", "status", default=None)
    normalized = _safe_status(status, "")
    if normalized:
        return normalized
    if _safe_bool(_value(source, "is_stale", "stale", default=None)):
        return DATA_STATUS_STALE
    if _safe_bool(_value(_metadata(source), "is_stale", "stale", default=None)):
        return DATA_STATUS_STALE
    return default


def _freshness(source: Any, *, default_status: str = "unknown") -> Dict[str, Any]:
    status = _data_status(source, default_status)
    metadata = _metadata(source)
    stale = _safe_bool(
        _value(source, "is_stale", "stale", default=None),
        _safe_bool(_value(metadata, "is_stale", "stale", default=None)),
    )
    result: Dict[str, Any] = {
        "status": status,
        "data_status": status if status != "ok" else "fresh",
        "is_stale": stale or status == DATA_STATUS_STALE,
    }
    for output, keys in (
        ("source", ("source",)),
        ("fetched_at", ("fetched_at",)),
        ("captured_at", ("captured_at",)),
        ("stale_reason", ("stale_reason",)),
        ("error_code", ("error_code",)),
        ("status_code", ("status_code",)),
        ("attempts", ("attempts",)),
    ):
        value = _value(source, *keys, default=None)
        if value is None:
            value = _value(metadata, *keys, default=None)
        if output in {"status_code", "attempts"}:
            value = _safe_int(value)
        elif output in {"source", "fetched_at", "captured_at", "stale_reason", "error_code"}:
            value = _safe_text(value, "") or None
            if output == "error_code" and value not in _KNOWN_ERROR_CODES:
                value = "monitor_error" if value else None
            if output == "stale_reason" and value and value not in _KNOWN_ERROR_CODES and value != "age":
                value = "unavailable"
        if value is not None:
            result[output] = value
    return result


def _error_freshness(error: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "status": DATA_STATUS_ERROR,
        "data_status": DATA_STATUS_ERROR,
        "is_stale": False,
        "error_code": _safe_error_code(error),
    }
    status_code = _safe_int(getattr(error, "status_code", None))
    attempts = _safe_int(getattr(error, "attempts", None))
    if status_code is not None:
        result["status_code"] = status_code
    if attempts is not None:
        result["attempts"] = attempts
    return result


def _warning(clan_tag: str, operation: str, code: str) -> str:
    safe_code = code if code in _KNOWN_ERROR_CODES or code in _KNOWN_STATUS_VALUES else "monitor_error"
    return f"{clan_tag}: {operation} status={safe_code}."


def _new_report() -> Dict[str, Any]:
    return {
        "ok": False,
        "processed_clans": 0,
        "snapshots_written": 0,
        "events_created": 0,
        "notifications_pending": 0,
        "freshness": {},
        "warnings": [],
        "clans": [],
    }


def _storage_result_ok(result: Any) -> bool:
    if not isinstance(result, Mapping):
        return False
    if result.get("ok") is False:
        return False
    status = _safe_text(result.get("status")).lower()
    return bool(result.get("ok") is True or status in {"ok", "fresh", "empty"})


def _storage_count(result: Any, fallback: int = 0) -> int:
    if isinstance(result, Mapping):
        for key in ("rows_written", "rows_upserted"):
            count = _safe_int(result.get(key))
            if count is not None and count >= 0:
                return count
    return fallback


def _aggregate_status(results: Sequence[Any], default: str = "empty") -> str:
    statuses = [_data_status(item, "unknown") for item in results]
    if not statuses:
        return default
    if DATA_STATUS_ERROR in statuses or DATA_STATUS_INVALID in statuses:
        return DATA_STATUS_ERROR
    if DATA_STATUS_STALE in statuses:
        return DATA_STATUS_STALE
    if DATA_STATUS_PARTIAL in statuses:
        return DATA_STATUS_PARTIAL
    if "fresh" in statuses or "ok" in statuses:
        return "fresh"
    if DATA_STATUS_EMPTY in statuses:
        return DATA_STATUS_EMPTY
    return "unknown"


def _configured_clans(
    clan_configs: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Tuple[MonitorClanConfig, ...]:
    source = CLAN_CONFIGS if clan_configs is None else clan_configs
    result: List[MonitorClanConfig] = []
    for config_key, supplied in source.items():
        supplied_mapping = supplied if isinstance(supplied, Mapping) else {}
        repository_config = get_clan_config(str(config_key))
        raw_tag = supplied_mapping.get("tag", repository_config.get("tag", config_key))
        tag = normalize_tag(str(raw_tag or ""))
        name = _safe_text(
            supplied_mapping.get("name", repository_config.get("name")),
            tag,
        )
        if not tag:
            raise MonitorConfigurationError("Repository clan configuration contains an invalid tag.")
        result.append(MonitorClanConfig(tag=tag, name=name))
    return tuple(result)


def _default_monitor_functions() -> MonitorFunctions:
    return MonitorFunctions(
        write_live_snapshots=write_live_snapshots,
        read_previous_player_snapshot=read_previous_player_snapshot,
        read_day_event=read_day_event,
        write_day_events=write_day_events,
        write_notification_logs=write_notification_logs,
    )


def _storage_callable(
    storage: Any,
    name: str,
    override: Optional[Callable[..., Mapping[str, Any]]],
    default: Optional[Callable[..., Mapping[str, Any]]],
) -> Optional[Callable[..., Mapping[str, Any]]]:
    if override is not None:
        return override
    if storage is not None:
        candidate = storage.get(name) if isinstance(storage, Mapping) else getattr(storage, name, None)
        if candidate is None:
            return None
        if not callable(candidate):
            raise MonitorConfigurationError(f"Injected storage function {name} is not callable.")
        return candidate
    return default


def _resolve_monitor_functions(
    storage: Any,
    *,
    write_live_snapshots_fn: Optional[Callable[..., Mapping[str, Any]]],
    read_previous_snapshot_fn: Optional[Callable[..., Mapping[str, Any]]],
    read_day_event_fn: Optional[Callable[..., Mapping[str, Any]]],
    write_day_events_fn: Optional[Callable[..., Mapping[str, Any]]],
    write_notification_logs_fn: Optional[Callable[..., Mapping[str, Any]]],
) -> MonitorFunctions:
    defaults = _default_monitor_functions()
    resolved = MonitorFunctions(
        write_live_snapshots=_storage_callable(
            storage,
            "write_live_snapshots",
            write_live_snapshots_fn,
            defaults.write_live_snapshots,
        ),
        read_previous_player_snapshot=_storage_callable(
            storage,
            "read_previous_player_snapshot",
            read_previous_snapshot_fn,
            defaults.read_previous_player_snapshot,
        ),
        read_day_event=_storage_callable(
            storage,
            "read_day_event",
            read_day_event_fn,
            defaults.read_day_event,
        ),
        write_day_events=_storage_callable(
            storage,
            "write_day_events",
            write_day_events_fn,
            defaults.write_day_events,
        ),
        write_notification_logs=_storage_callable(
            storage,
            "write_notification_logs",
            write_notification_logs_fn,
            defaults.write_notification_logs,
        ),
    )
    if any(
        function is None
        for function in (
            resolved.write_live_snapshots,
            resolved.read_previous_player_snapshot,
            resolved.write_day_events,
        )
    ):
        raise MonitorConfigurationError("Injected monitor storage is incomplete.")
    return resolved


def determine_race_activity(race: Any) -> Dict[str, Any]:
    """Return a conservative, testable active/inactive race decision."""

    status = _data_status(race, DATA_STATUS_EMPTY)
    context = _value(race, "context", default=None)
    if status == DATA_STATUS_STALE or _safe_bool(_value(race, "is_stale", default=False)):
        return {"status": DATA_STATUS_STALE, "reason": "current race data is stale"}
    if status in {DATA_STATUS_ERROR, DATA_STATUS_INVALID}:
        return {"status": DATA_STATUS_ERROR, "reason": "current race data is invalid"}
    if status == DATA_STATUS_EMPTY or context is None:
        return {"status": DATA_STATUS_EMPTY, "reason": "current race data is empty"}

    state = _safe_text(_value(context, "state", default=""))
    state_token = re.sub(r"[^a-z0-9]", "", state.lower())
    period_type = _safe_text(_value(context, "period_type", "periodType", default=""))
    period_token = re.sub(r"[^a-z0-9]", "", period_type.lower())
    identity_fields = (
        _value(context, "season_id", "seasonId", default=None),
        _value(context, "section_index", "sectionIndex", default=None),
        _value(context, "period_index", "periodIndex", default=None),
        _value(context, "race_created_at", "raceCreatedAt", default=None),
    )
    if state_token in _ACTIVE_RACE_STATES and all(value is not None for value in identity_fields):
        if period_token in _INACTIVE_PERIOD_TYPES:
            return {"status": "inactive", "reason": "race period is non-competitive"}
        return {
            "status": "active",
            "reason": "recognized active River Race state",
            "state": state,
            "period_type": period_type or None,
        }
    if state_token in _ACTIVE_RACE_STATES:
        return {"status": DATA_STATUS_ERROR, "reason": "active race identity is incomplete"}
    return {
        "status": "inactive",
        "reason": "current race state is not an active River Race day",
        "state": state or None,
        "period_type": period_type or None,
    }


def build_live_snapshot_rows(
    clan_tag: str,
    race: Any,
    captured_at: str,
) -> List[Dict[str, Any]]:
    """Build T05 rows from T02 models without inventing metric defaults."""

    context = _value(race, "context", default=None)
    if context is None:
        return []
    rows: List[Dict[str, Any]] = []
    race_source = _safe_text(_value(race, "source", default=""), "royaleapi_proxy")
    participants = _value(race, "participants", default=()) or ()
    for participant in participants:
        player_tag = _safe_text(_value(participant, "player_tag", "tag", default=""))
        if not player_tag:
            continue
        player_name = _safe_text(_value(participant, "name", default=""), player_tag)
        rows.append(
            {
                "clan_tag": clan_tag,
                "season_id": _value(context, "season_id", "seasonId"),
                "section_index": _value(context, "section_index", "sectionIndex"),
                "period_index": _value(context, "period_index", "periodIndex"),
                "period_type": _safe_text(
                    _value(context, "period_type", "periodType", default=""),
                    "unknown",
                ),
                "race_created_at": _value(
                    context,
                    "race_created_at",
                    "raceCreatedAt",
                ),
                "player_tag": player_tag,
                "player_name": player_name,
                "player_role": _safe_text(_value(participant, "role", default="")),
                "decks_used": _value(participant, "decks_used", "decksUsed"),
                "decks_used_today": _value(
                    participant,
                    "decks_used_today",
                    "decksUsedToday",
                ),
                "fame": _value(participant, "fame"),
                "repair_points": _value(participant, "repair_points", "repairPoints"),
                "boat_attacks": _value(participant, "boat_attacks", "boatAttacks"),
                "boat_attacks_today": _value(
                    participant,
                    "boat_attacks_today",
                    "boatAttacksToday",
                ),
                "boat_defenses": _value(participant, "boat_defenses", "boatDefenses"),
                "boat_defenses_today": _value(
                    participant,
                    "boat_defenses_today",
                    "boatDefensesToday",
                ),
                "captured_at": captured_at,
                "source": race_source,
                "payload_version": 1,
            }
        )
    return rows


def _new_event_key_set(result: Any) -> bool:
    if not isinstance(result, Mapping):
        return False
    event = result.get("event")
    if isinstance(event, Mapping):
        return True
    if result.get("exists") is True or result.get("event_exists") is True:
        return True
    return _data_status(result, "unknown") in {"fresh", DATA_STATUS_STALE} and event is not None


def _policy_allows(policy: Any, event: Mapping[str, Any]) -> bool:
    selected = policy
    if selected is None:
        selected = os.environ.get(NOTIFICATION_POLICY_ENV, "").strip().lower()
    if callable(selected):
        try:
            return bool(selected(event))
        except Exception:
            return False
    if isinstance(selected, bool):
        return selected
    return _safe_text(selected).lower() in {"1", "enabled", "on", "pending", "true"}


def _policy_is_enabled(policy: Any) -> bool:
    selected = policy
    if selected is None:
        selected = os.environ.get(NOTIFICATION_POLICY_ENV, "").strip().lower()
    if callable(selected):
        return True
    if isinstance(selected, bool):
        return selected
    return _safe_text(selected).lower() in {"1", "enabled", "on", "pending", "true"}


def _event_args(
    clan_tag: str,
    context: Any,
    participant: Any,
    previous: Any,
    observed_at: str,
    race_day_key: str,
    *,
    existing_event_keys: Iterable[str] = (),
    api_stale: bool = False,
) -> Dict[str, Any]:
    previous_row = _value(previous, "snapshot", default=None)
    if previous_row is None and isinstance(previous, Mapping):
        previous_row = previous
    previous_decks = _value(
        previous_row,
        "decks_used_today",
        "decksUsedToday",
        default=None,
    )
    return {
        "clan_tag": clan_tag,
        "season_id": _value(context, "season_id", "seasonId"),
        "section_index": _value(context, "section_index", "sectionIndex"),
        "race_created_at": _value(context, "race_created_at", "raceCreatedAt"),
        "period_index": _value(context, "period_index", "periodIndex"),
        "player_tag": _value(participant, "player_tag", "tag"),
        "current_decks_used_today": _value(
            participant,
            "decks_used_today",
            "decksUsedToday",
            default=None,
        ),
        "previous_decks_used_today": previous_decks,
        "previous_race_day_key": race_day_key if previous_row is not None else None,
        "observed_at": observed_at,
        "api_stale": api_stale,
        "existing_event_keys": tuple(existing_event_keys),
    }


def _process_clan(
    config: MonitorClanConfig,
    client: Any,
    functions: MonitorFunctions,
    observed_at: str,
    notification_policy: Any,
) -> Dict[str, Any]:
    tag = config.tag
    summary: Dict[str, Any] = {
        "ok": True,
        "status": "pending",
        "clan_tag": tag,
        "clan_name": config.name,
        "snapshots_written": 0,
        "events_created": 0,
        "notifications_pending": 0,
        "freshness": {},
        "warnings": [],
    }
    issue_found = False

    responses: Dict[str, Any] = {}
    methods = (
        ("clan", "get_clan"),
        ("members", "get_members"),
        ("current_river_race", "get_current_river_race"),
    )
    for operation, method_name in methods:
        try:
            method = getattr(client, method_name)
            responses[operation] = method(tag)
            summary["freshness"][operation] = _freshness(responses[operation], default_status="unknown")
        except Exception as error:
            issue_found = True
            responses[operation] = None
            summary["freshness"][operation] = _error_freshness(error)
            summary["warnings"].append(_warning(tag, operation, _safe_error_code(error)))

    normalized_clan = None
    if responses["clan"] is not None:
        try:
            normalized_clan = normalize_clan(responses["clan"], clan_tag=tag)
            summary["freshness"]["clan"] = _freshness(normalized_clan, default_status="unknown")
            if _data_status(normalized_clan) in {DATA_STATUS_STALE, DATA_STATUS_INVALID, DATA_STATUS_ERROR}:
                issue_found = True
                summary["warnings"].append(
                    _warning(tag, "clan_normalization", _data_status(normalized_clan))
                )
        except Exception as error:
            issue_found = True
            summary["freshness"]["clan"] = _error_freshness(error)
            summary["warnings"].append(_warning(tag, "clan_normalization", "normalization_error"))

    normalized_members: Any = ()
    if responses["members"] is not None:
        try:
            normalized_members = normalize_members(responses["members"], clan_tag=tag)
            members_status = _data_status(responses["members"], DATA_STATUS_EMPTY)
            summary["freshness"]["members"] = _freshness(
                normalized_members,
                default_status=members_status,
            )
            if members_status in {DATA_STATUS_STALE, DATA_STATUS_INVALID, DATA_STATUS_ERROR}:
                issue_found = True
                summary["warnings"].append(_warning(tag, "members", members_status))
        except Exception as error:
            issue_found = True
            normalized_members = ()
            summary["freshness"]["members"] = _error_freshness(error)
            summary["warnings"].append(_warning(tag, "members_normalization", "normalization_error"))
    clan_name = _safe_text(_value(normalized_clan, "name", default=""), config.name)
    summary["clan_name"] = clan_name

    if responses["current_river_race"] is None:
        summary["status"] = "error"
        summary["ok"] = False
        summary["race"] = {"status": DATA_STATUS_ERROR, "reason": "current race request failed"}
        return summary

    try:
        race = normalize_current_river_race(
            responses["current_river_race"],
            clan_tag=tag,
            members=normalized_members,
        )
        summary["freshness"]["current_river_race"] = _freshness(race, default_status=DATA_STATUS_EMPTY)
    except Exception as error:
        summary["status"] = "error"
        summary["ok"] = False
        summary["freshness"]["current_river_race"] = _error_freshness(error)
        summary["warnings"].append(_warning(tag, "race_normalization", "normalization_error"))
        summary["race"] = {"status": DATA_STATUS_ERROR, "reason": "current race normalization failed"}
        return summary

    race_data_status = _data_status(race, DATA_STATUS_EMPTY)
    if race_data_status == DATA_STATUS_PARTIAL:
        issue_found = True
        summary["warnings"].append(_warning(tag, "current_river_race", DATA_STATUS_PARTIAL))

    activity = determine_race_activity(race)
    context = _value(race, "context", default=None)
    summary["race"] = {
        "status": activity["status"],
        "reason": activity["reason"],
        "state": _value(context, "state", default=None),
        "period_type": _value(context, "period_type", "periodType", default=None),
        "season_id": _value(context, "season_id", "seasonId", default=None),
        "section_index": _value(context, "section_index", "sectionIndex", default=None),
        "period_index": _value(context, "period_index", "periodIndex", default=None),
        "race_created_at": _value(context, "race_created_at", "raceCreatedAt", default=None),
    }

    if activity["status"] != "active":
        if activity["status"] in {DATA_STATUS_STALE, DATA_STATUS_ERROR}:
            issue_found = True
            summary["ok"] = False
            summary["warnings"].append(_warning(tag, "current_river_race", activity["status"]))
            summary["status"] = activity["status"]
        else:
            summary["status"] = "partial" if issue_found else activity["status"]
            summary["ok"] = not issue_found
            summary["warnings"].append(_warning(tag, "current_river_race", activity["status"]))
        return summary

    try:
        race_day_key = build_race_day_key(
            tag,
            _value(context, "season_id", "seasonId"),
            _value(context, "section_index", "sectionIndex"),
            _value(context, "race_created_at", "raceCreatedAt"),
            _value(context, "period_index", "periodIndex"),
        )
    except DuelFirstValidationError:
        summary["status"] = "error"
        summary["ok"] = False
        summary["warnings"].append(_warning(tag, "race_identity", "invalid_request"))
        return summary
    summary["race"]["race_day_key"] = race_day_key
    summary["race"]["race_key"] = race_day_key.rsplit("|", 1)[0]

    snapshot_rows = build_live_snapshot_rows(tag, race, observed_at)
    try:
        snapshot_result = functions.write_live_snapshots(snapshot_rows)
        summary["freshness"]["live_snapshots"] = _freshness(snapshot_result, default_status="empty")
        if not _storage_result_ok(snapshot_result):
            issue_found = True
            summary["warnings"].append(_warning(tag, "live_snapshots", _data_status(snapshot_result, DATA_STATUS_ERROR)))
        else:
            summary["snapshots_written"] = _storage_count(snapshot_result, len(snapshot_rows))
    except Exception as error:
        issue_found = True
        summary["freshness"]["live_snapshots"] = _error_freshness(error)
        summary["warnings"].append(_warning(tag, "live_snapshots", _safe_error_code(error)))

    previous_results: List[Any] = []
    event_read_results: List[Any] = []
    event_candidates: List[Mapping[str, Any]] = []
    notification_candidates: List[Mapping[str, Any]] = []
    participants = _value(race, "participants", default=()) or ()
    for participant in participants:
        raw_player_tag = _safe_text(_value(participant, "player_tag", "tag", default=""))
        if not raw_player_tag:
            continue
        try:
            player_tag = normalize_tag(raw_player_tag)
        except Exception:
            issue_found = True
            summary["warnings"].append(_warning(tag, "participant", "invalid_tag"))
            continue
        try:
            previous = functions.read_previous_player_snapshot(
                tag,
                _value(context, "race_created_at", "raceCreatedAt"),
                _value(context, "period_index", "periodIndex"),
                player_tag,
                before_captured_at=observed_at,
            )
        except Exception as error:
            issue_found = True
            previous = {"ok": False, "status": DATA_STATUS_ERROR, "error_code": _safe_error_code(error)}
            summary["warnings"].append(_warning(tag, "previous_snapshot", _safe_error_code(error)))
        previous_results.append(previous)
        previous_status = _data_status(previous, DATA_STATUS_EMPTY)
        if previous_status in {DATA_STATUS_ERROR, DATA_STATUS_INVALID, DATA_STATUS_STALE}:
            issue_found = True
            summary["warnings"].append(_warning(tag, "previous_snapshot", previous_status))
            continue

        api_stale = _data_status(participant, "unknown") == DATA_STATUS_STALE
        args = _event_args(
            tag,
            context,
            participant,
            previous,
            observed_at,
            race_day_key,
            api_stale=api_stale,
        )
        try:
            classification = observe_duel_first(**args)
        except DuelFirstValidationError:
            issue_found = True
            summary["warnings"].append(_warning(tag, "duel_first", "invalid_request"))
            continue

        if not classification.event or not classification.event_key:
            continue

        existing = None
        if functions.read_day_event is not None:
            try:
                existing = functions.read_day_event(
                    tag,
                    _value(context, "race_created_at", "raceCreatedAt"),
                    _value(context, "period_index", "periodIndex"),
                    player_tag,
                    classification.event["event_type"],
                )
                event_read_results.append(existing)
            except Exception as error:
                issue_found = True
                event_read_results.append(
                    {"ok": False, "status": DATA_STATUS_ERROR, "error_code": _safe_error_code(error)}
                )
                summary["warnings"].append(_warning(tag, "day_event_read", _safe_error_code(error)))

        if _new_event_key_set(existing):
            try:
                classification = observe_duel_first(
                    **_event_args(
                        tag,
                        context,
                        participant,
                        previous,
                        observed_at,
                        race_day_key,
                        existing_event_keys=(classification.event_key,),
                        api_stale=api_stale,
                    )
                )
            except DuelFirstValidationError:
                issue_found = True
                summary["warnings"].append(_warning(tag, "duel_first", "invalid_request"))
                continue

        if classification.new_event and classification.event:
            event_candidates.append(classification.event)
            if _policy_allows(notification_policy, classification.event):
                notification_candidates.append(
                    {
                        "event_key": classification.event_key,
                        "channel": NOTIFICATION_CHANNEL,
                        "status": "pending",
                        "details": {
                            "event_type": classification.event.get("event_type"),
                            "race_day_key": classification.event.get("details", {}).get("race_day_key"),
                        },
                    }
                )

    summary["freshness"]["previous_player_snapshots"] = {
        "status": _aggregate_status(previous_results),
        "observations": len(previous_results),
    }
    if event_read_results:
        summary["freshness"]["day_event_reads"] = {
            "status": _aggregate_status(event_read_results),
            "observations": len(event_read_results),
        }

    if event_candidates:
        try:
            event_result = functions.write_day_events(event_candidates)
            summary["freshness"]["day_events"] = _freshness(event_result, default_status="empty")
            if not _storage_result_ok(event_result):
                issue_found = True
                summary["warnings"].append(_warning(tag, "day_events", _data_status(event_result, DATA_STATUS_ERROR)))
            else:
                summary["events_created"] = min(
                    len(event_candidates),
                    _storage_count(event_result, len(event_candidates)),
                )
        except Exception as error:
            issue_found = True
            summary["freshness"]["day_events"] = _error_freshness(error)
            summary["warnings"].append(_warning(tag, "day_events", _safe_error_code(error)))
    else:
        summary["freshness"]["day_events"] = {"status": "empty", "data_status": "empty", "is_stale": False}

    if notification_candidates:
        if functions.write_notification_logs is None:
            issue_found = True
            summary["freshness"]["notification_queue"] = _error_freshness(
                MonitorConfigurationError("Notification queue is not configured.")
            )
            summary["warnings"].append(_warning(tag, "notification_queue", "configuration_error"))
        else:
            try:
                notification_result = functions.write_notification_logs(notification_candidates)
                summary["freshness"]["notification_queue"] = _freshness(
                    notification_result,
                    default_status="empty",
                )
                if not _storage_result_ok(notification_result):
                    issue_found = True
                    summary["warnings"].append(
                        _warning(tag, "notification_queue", _data_status(notification_result, DATA_STATUS_ERROR))
                    )
                else:
                    summary["notifications_pending"] = min(
                        len(notification_candidates),
                        _storage_count(notification_result, len(notification_candidates)),
                    )
            except Exception as error:
                issue_found = True
                summary["freshness"]["notification_queue"] = _error_freshness(error)
                summary["warnings"].append(_warning(tag, "notification_queue", _safe_error_code(error)))
    else:
        summary["freshness"]["notification_queue"] = {
            "status": "empty" if _policy_is_enabled(notification_policy) else "disabled",
            "data_status": "empty",
            "is_stale": False,
        }

    summary["status"] = "partial" if issue_found else "ok"
    summary["ok"] = not issue_found
    return summary


def run_war_monitor(
    *,
    client: Any = None,
    client_factory: Optional[Callable[[], Any]] = None,
    storage: Any = None,
    clan_configs: Optional[Mapping[str, Mapping[str, Any]]] = None,
    clock: Optional[Callable[[], Any]] = None,
    observed_at: Any = None,
    notification_policy: Any = None,
    write_live_snapshots_fn: Optional[Callable[..., Mapping[str, Any]]] = None,
    read_previous_snapshot_fn: Optional[Callable[..., Mapping[str, Any]]] = None,
    read_day_event_fn: Optional[Callable[..., Mapping[str, Any]]] = None,
    write_day_events_fn: Optional[Callable[..., Mapping[str, Any]]] = None,
    write_notification_logs_fn: Optional[Callable[..., Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run one complete monitor pass and return the stable response contract."""

    report = _new_report()
    configs = _configured_clans(clan_configs)
    report["processed_clans"] = len(configs)
    if not configs:
        raise MonitorConfigurationError("No repository clans are configured.")

    functions = _resolve_monitor_functions(
        storage,
        write_live_snapshots_fn=write_live_snapshots_fn,
        read_previous_snapshot_fn=read_previous_snapshot_fn,
        read_day_event_fn=read_day_event_fn,
        write_day_events_fn=write_day_events_fn,
        write_notification_logs_fn=write_notification_logs_fn,
    )

    if client is None:
        if client_factory is not None:
            try:
                client = client_factory()
            except Exception:
                raise MonitorConfigurationError("Clash client could not be constructed.") from None
        else:
            api_key = os.environ.get("CLASH_ROYALE_API_KEY", "").strip()
            if not api_key:
                raise MonitorConfigurationError("CLASH_ROYALE_API_KEY is not configured.")
            try:
                client = ClashRoyaleClient(api_key=api_key)
            except Exception:
                raise MonitorConfigurationError("Clash client could not be configured.") from None

    if observed_at is not None:
        capture_time = _utc_timestamp(observed_at)
    else:
        capture_time = _utc_timestamp(clock() if clock is not None else None)

    for config in configs:
        try:
            clan_summary = _process_clan(
                config,
                client,
                functions,
                capture_time,
                notification_policy,
            )
        except Exception:
            clan_summary = {
                "ok": False,
                "status": "error",
                "clan_tag": config.tag,
                "clan_name": config.name,
                "snapshots_written": 0,
                "events_created": 0,
                "notifications_pending": 0,
                "freshness": {"monitor": _error_freshness(RuntimeError())},
                "warnings": [_warning(config.tag, "clan_processing", "monitor_error")],
            }
        report["clans"].append(clan_summary)
        report["freshness"][config.tag] = clan_summary["freshness"]
        report["warnings"].extend(clan_summary["warnings"])
        report["snapshots_written"] += _safe_int(clan_summary.get("snapshots_written")) or 0
        report["events_created"] += _safe_int(clan_summary.get("events_created")) or 0
        report["notifications_pending"] += _safe_int(clan_summary.get("notifications_pending")) or 0

    report["ok"] = all(bool(clan.get("ok")) for clan in report["clans"])
    report["http_status"] = 200 if report["ok"] else HTTP_STATUS_PARTIAL
    return report


# Short aliases keep the orchestration seam easy to find for tests/callers.
run_monitor = run_war_monitor
process_war_monitor = run_war_monitor


def _failure_report(error: str) -> Dict[str, Any]:
    report = _new_report()
    report["error"] = error
    report["http_status"] = 500
    return report


class handler(BaseHTTPRequestHandler):
    """Vercel Python function handler for ``POST /api/war_monitor``."""

    def _send_json(self, status_code: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        expected = configured_monitor_secret()
        if not expected:
            self._send_json(500, _failure_report("Monitor secret is not configured."))
            return
        provided = self.headers.get(MONITOR_SECRET_HEADER, "")
        if not secret_matches(provided, expected):
            unauthorized = _failure_report("Unauthorized")
            unauthorized["http_status"] = 401
            self._send_json(401, unauthorized)
            return

        try:
            report = run_war_monitor()
            status_code = _safe_int(report.get("http_status")) or (200 if report.get("ok") else HTTP_STATUS_PARTIAL)
            report.pop("http_status", None)
            self._send_json(status_code, report)
        except MonitorConfigurationError:
            self._send_json(500, _failure_report("Monitor configuration error."))
        except Exception:
            self._send_json(500, _failure_report("Monitor run failed."))

    def do_GET(self) -> None:
        response = _failure_report("Method not allowed")
        response["http_status"] = 405
        self._send_json(405, response)


__all__ = [
    "HTTP_STATUS_PARTIAL",
    "MONITOR_SECRET_ENV",
    "MONITOR_SECRET_HEADER",
    "MonitorClanConfig",
    "MonitorConfigurationError",
    "MonitorFunctions",
    "build_live_snapshot_rows",
    "configured_monitor_secret",
    "determine_race_activity",
    "handler",
    "process_war_monitor",
    "run_monitor",
    "run_war_monitor",
    "secret_matches",
]
