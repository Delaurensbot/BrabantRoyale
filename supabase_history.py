"""Supabase-backed history and server-only live storage for clan-war analytics.

The raw live tables intentionally have no automatic retention delete here.  A
short-retention policy needs a scheduler or a database-side retention job; this
small I/O layer cannot safely prove that such a job is present.  It therefore
keeps writes idempotent and leaves retention operations to a separately
approved operational change.
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import unquote, urlsplit

import requests

try:
    from api.clash_client import ClashRoyaleClient, normalize_tag as normalize_api_tag
except ImportError:  # pragma: no cover - useful when loaded as a loose file.
    from clash_client import ClashRoyaleClient, normalize_tag as normalize_api_tag
HISTORY_TABLE = "clan_war_player_weeks"
EXCLUSIONS_TABLE = "clan_war_week_exclusions"
LIVE_SNAPSHOT_TABLE = "river_race_live_snapshots"
DAY_EVENTS_TABLE = "war_player_day_events"
NOTIFICATION_LOG_TABLE = "notification_log"
ROSTER_SNAPSHOT_TABLE = "clan_roster_snapshots"
DEFAULT_PAGE_SIZE = 1000
DEFAULT_WRITE_BATCH_SIZE = 500
DEFAULT_RAW_STORAGE_TIMEOUT = 25
DEFAULT_RAW_STORAGE_MAX_RETRIES = 2
DEFAULT_RAW_STORAGE_BACKOFF_SECONDS = 0.25
DEFAULT_RAW_STORAGE_MAX_BACKOFF_SECONDS = 5.0
DEFAULT_SUPABASE_URL = "https://upbjlamddxooxhxhkivg.supabase.co"
DEFAULT_SUPABASE_PUBLISHABLE_KEY = (
    "sb_publishable_gWj42LLCw4odVjLdRecWrw_4xeQlF9i"
)

LIVE_SNAPSHOT_CONFLICT_COLUMNS = (
    "clan_tag",
    "race_created_at",
    "period_index",
    "player_tag",
    "capture_bucket",
)
DAY_EVENT_CONFLICT_COLUMNS = (
    "clan_tag",
    "race_created_at",
    "period_index",
    "player_tag",
    "event_type",
)
NOTIFICATION_LOG_CONFLICT_COLUMNS = ("event_key", "channel")
ROSTER_SNAPSHOT_CONFLICT_COLUMNS = (
    "clan_tag",
    "player_tag",
    "captured_at",
)

LIVE_SNAPSHOT_SELECT_COLUMNS = (
    "id,clan_tag,season_id,section_index,period_index,period_type,"
    "race_created_at,player_tag,player_name,player_role,decks_used,"
    "decks_used_today,fame,repair_points,boat_attacks,boat_attacks_today,"
    "boat_defenses,boat_defenses_today,captured_at,source,payload_version,"
    "capture_bucket"
)
DAY_EVENT_SELECT_COLUMNS = (
    "id,clan_tag,race_created_at,period_index,player_tag,event_type,"
    "observed_decks_used_today,confidence,observed_at,details,created_at"
)
NOTIFICATION_LOG_SELECT_COLUMNS = (
    "id,event_key,channel,status,response_code,sent_at,details"
)
ROSTER_SNAPSHOT_SELECT_COLUMNS = (
    "id,clan_tag,player_tag,player_name,role,trophies,seen_at,captured_at"
)

STORAGE_STATUS_OK = "ok"
STORAGE_STATUS_EMPTY = "empty"
STORAGE_STATUS_FRESH = "fresh"
STORAGE_STATUS_STALE = "stale"
STORAGE_STATUS_PARTIAL = "partial"
STORAGE_STATUS_ERROR = "error"

DEFAULT_ROSTER_MAX_ROWS = 10000

_MISSING = object()
_RETRYABLE_STORAGE_STATUSES = frozenset({408, 425, 429})


class SupabaseStorageError(RuntimeError):
    """Safe metadata for a failed raw-table request.

    The exception deliberately stores no URL, headers, JWT, response body, or
    request payload.  Public write/read functions convert it to a small result
    dictionary so future monitor code can branch on ``status`` and ``error``.
    """

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
        "configuration_error": (
            "Supabase server configuration is missing or invalid."
        ),
        "server_key_required": (
            "A Supabase server key is required for raw monitoring storage."
        ),
        "timeout": "Supabase storage request timed out.",
        "transport_error": "Supabase storage request failed temporarily.",
        "rate_limited": "Supabase storage is rate limited.",
        "upstream_server_error": (
            "Supabase storage is temporarily unavailable."
        ),
        "supabase_http_error": "Supabase storage request was rejected.",
        "invalid_response": "Supabase storage returned an invalid response.",
        "invalid_payload": "Supabase storage payload is not JSON serializable.",
    }.get(code, "Supabase storage request failed.")


def normalize_tag(value: object) -> str:
    return str(value or "").strip().replace("%23", "").replace("#", "").upper()


def _strict_roster_tag(value: object) -> Optional[str]:
    """Normalize a roster identity without silently merging malformed tags."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    for _ in range(2):
        decoded = unquote(candidate)
        if decoded == candidate:
            break
        candidate = decoded
    if candidate.startswith("#"):
        candidate = candidate[1:]
    if not re.fullmatch(r"[A-Za-z0-9]{1,32}", candidate):
        return None
    return candidate.upper()


def _supabase_headers(
    api_key: str,
    *,
    write: bool = False,
    ingest_token: Optional[str] = None,
) -> Dict[str, str]:
    headers = {
        "Accept": "application/json",
        "apikey": api_key,
    }
    # New sb_publishable_/sb_secret_ keys must only be sent as `apikey`.
    # Legacy anon/service_role JWT keys still require the Bearer header.
    if not api_key.startswith("sb_"):
        headers["Authorization"] = f"Bearer {api_key}"
    if write:
        headers["Content-Type"] = "application/json"
        headers["Prefer"] = "resolution=merge-duplicates,return=minimal"
        if ingest_token:
            headers["X-Ingest-Token"] = ingest_token
    return headers


def _jwt_role(api_key: str) -> Optional[str]:
    """Read a JWT role without retaining or exposing the token."""

    parts = api_key.split(".")
    if len(parts) != 3:
        return None
    try:
        padded_payload = parts[1] + ("=" * (-len(parts[1]) % 4))
        payload = json.loads(
            base64.urlsafe_b64decode(padded_payload.encode("ascii"))
        )
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return None
    role = payload.get("role") if isinstance(payload, dict) else None
    return str(role).strip().lower() if role else None


def _is_public_supabase_key(api_key: str) -> bool:
    normalized = api_key.strip().lower()
    if normalized.startswith(("sb_publishable_", "sb_anon_")):
        return True
    if normalized in {"anon", "authenticated", "publishable"}:
        return True
    if normalized.startswith("anon_"):
        return True
    return _jwt_role(api_key) in {"anon", "authenticated"}


def get_supabase_read_config() -> Optional[Tuple[str, str]]:
    url = (
        os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
        or DEFAULT_SUPABASE_URL
    )
    api_key = (
        os.environ.get("SUPABASE_PUBLISHABLE_KEY", "").strip()
        or os.environ.get("SUPABASE_ANON_KEY", "").strip()
        or os.environ.get("SUPABASE_SECRET_KEY", "").strip()
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        or DEFAULT_SUPABASE_PUBLISHABLE_KEY
    )
    return (url, api_key) if url and api_key else None


def get_supabase_server_config() -> Optional[Tuple[str, str]]:
    """Return the server-only Supabase URL/key pair, if configured.

    This resolver intentionally never falls back to publishable or anon keys.
    ``SUPABASE_SECRET_KEY`` is preferred for current Supabase projects, while
    ``SUPABASE_SERVICE_ROLE_KEY`` keeps legacy deployments compatible with the
    service_role grants in the T05 migration.
    """

    url = (
        os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
        or DEFAULT_SUPABASE_URL
    )
    for env_name in ("SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_ROLE_KEY"):
        api_key = os.environ.get(env_name, "").strip()
        if api_key and not _is_public_supabase_key(api_key):
            return (url, api_key) if url else None
    return None


def get_supabase_write_config() -> Tuple[str, str, Optional[str]]:
    url = (
        os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
        or DEFAULT_SUPABASE_URL
    )
    elevated_key = (
        os.environ.get("SUPABASE_SECRET_KEY", "").strip()
        or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    )
    if elevated_key:
        return url, elevated_key, None

    publishable_key = (
        os.environ.get("SUPABASE_PUBLISHABLE_KEY", "").strip()
        or os.environ.get("SUPABASE_ANON_KEY", "").strip()
        or DEFAULT_SUPABASE_PUBLISHABLE_KEY
    )
    ingest_token = os.environ.get("SUPABASE_INGEST_TOKEN", "").strip()
    if not ingest_token:
        raise RuntimeError(
            "Missing SUPABASE_INGEST_TOKEN environment variable "
            "(an elevated Supabase key is supported as a fallback)."
        )
    return url, publishable_key, ingest_token


def clash_date_to_datetime(value: object) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Race snapshot has no createdDate.")

    for pattern in ("%Y%m%dT%H%M%S.%fZ", "%Y%m%dT%H%M%SZ"):
        try:
            return datetime.strptime(raw, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Unsupported race createdDate: {raw}") from exc

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def clash_date_to_iso(value: object) -> str:
    return clash_date_to_datetime(value).isoformat().replace("+00:00", "Z")


def iso_to_clash_date(value: object) -> str:
    parsed = clash_date_to_datetime(value)
    return parsed.strftime("%Y%m%dT%H%M%S.000Z")


def extract_clan_participants(
    race: Dict[str, object],
    clan_tag: str,
) -> List[Dict[str, object]]:
    norm_tag = normalize_tag(clan_tag)
    standings = race.get("standings") or []
    for standing in standings:
        clan = standing.get("clan") or {}
        if normalize_tag(clan.get("tag")) == norm_tag:
            return list(clan.get("participants") or standing.get("participants") or [])

    for clan in race.get("clans") or []:
        if normalize_tag(clan.get("tag")) == norm_tag:
            return list(clan.get("participants") or [])

    return []


def fetch_clash_json(
    path: str,
    api_key: str,
    *,
    timeout: int = 25,
) -> Dict[str, object]:
    """Compatibility adapter backed by the central official API client."""

    client = ClashRoyaleClient(
        api_key=api_key,
        timeout=timeout,
        requester=requests.get,
    )
    parts = [unquote(part) for part in urlsplit(path).path.split("/") if part]
    if len(parts) != 3 or parts[0] != "clans":
        raise ValueError("Unsupported official Clash API path.")
    clan_tag = normalize_api_tag(parts[1])
    endpoint = parts[2].lower()
    if endpoint == "members":
        response = client.get_members(clan_tag)
    elif endpoint == "riverracelog":
        response = client.get_river_race_log(clan_tag)
    elif endpoint == "currentriverrace":
        response = client.get_current_river_race(clan_tag)
    else:
        raise ValueError("Unsupported official Clash API path.")
    return dict(response.data) if isinstance(response.data, Mapping) else {}


def fetch_live_clan_data(
    clan_tag: str,
    api_key: str,
) -> Tuple[List[Dict[str, object]], List[Dict[str, object]]]:
    norm_tag = normalize_api_tag(clan_tag)
    client = ClashRoyaleClient(
        api_key=api_key,
        requester=requests.get,
    )
    members_payload = client.get_members(norm_tag).data
    members = list(members_payload.get("items") or []) if isinstance(members_payload, Mapping) else []

    race_items: List[Dict[str, object]] = []
    river_log = client.get_river_race_log(norm_tag).data
    if isinstance(river_log, Mapping):
        race_items.extend(river_log.get("items") or [])

    try:
        current_race = client.get_current_river_race(norm_tag).data
        if isinstance(current_race, Mapping) and current_race:
            current_race = dict(current_race)
            current_race["is_current"] = True
            race_items.append(current_race)
    except Exception:
        # The endpoint can be unavailable between races. Completed log entries
        # must still be processed in that case.
        pass

    return members, race_items


def fetch_history_rows(
    clan_tag: str,
    *,
    supabase_url: str,
    api_key: str,
    player_tag: Optional[str] = None,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> List[Dict[str, object]]:
    norm_tag = normalize_tag(clan_tag)
    endpoint = f"{supabase_url.rstrip('/')}/rest/v1/{HISTORY_TABLE}"
    params = {
        "select": (
            "clan_tag,clan_name,season_id,race_created_at,player_tag,"
            "player_name,player_role,fame,repair_points,contribution,decks_used"
        ),
        "clan_tag": f"eq.{norm_tag}",
        "order": "race_created_at.asc,player_name.asc",
    }
    normalized_player_tag = normalize_tag(player_tag)
    if normalized_player_tag:
        params["player_tag"] = f"eq.{normalized_player_tag}"
    headers = _supabase_headers(api_key)
    rows: List[Dict[str, object]] = []
    start = 0

    while True:
        page_headers = {
            **headers,
            "Range-Unit": "items",
            "Range": f"{start}-{start + page_size - 1}",
        }
        response = requests.get(
            endpoint,
            params=params,
            headers=page_headers,
            timeout=25,
        )
        if response.status_code not in (200, 206):
            raise RuntimeError(
                f"Supabase history read failed with HTTP {response.status_code}."
            )

        page = response.json() if response.content else []
        if not isinstance(page, list):
            raise RuntimeError("Supabase history response was not a row list.")
        rows.extend(page)
        if len(page) < page_size:
            break
        start += page_size

    return rows


def fetch_week_exclusions(
    clan_tag: str,
    *,
    supabase_url: str,
    api_key: str,
    player_tag: Optional[str] = None,
) -> List[Dict[str, object]]:
    norm_tag = normalize_tag(clan_tag)
    endpoint = f"{supabase_url.rstrip('/')}/rest/v1/{EXCLUSIONS_TABLE}"
    params = {
        "select": (
            "clan_tag,race_created_at,player_tag,reason,created_at,updated_at"
        ),
        "clan_tag": f"eq.{norm_tag}",
        "order": "race_created_at.asc,player_tag.asc",
    }
    normalized_player_tag = normalize_tag(player_tag)
    if normalized_player_tag:
        params["player_tag"] = f"eq.{normalized_player_tag}"
    response = requests.get(
        endpoint,
        params=params,
        headers=_supabase_headers(api_key),
        timeout=25,
    )
    if response.status_code != 200:
        raise RuntimeError(
            "Supabase week exclusions read failed with "
            f"HTTP {response.status_code}."
        )

    rows = response.json() if response.content else []
    if not isinstance(rows, list):
        raise RuntimeError("Supabase week exclusions response was not a row list.")
    return rows


def history_rows_to_races(
    rows: Iterable[Dict[str, object]],
    clan_tag: str,
) -> List[Dict[str, object]]:
    norm_tag = normalize_tag(clan_tag)
    grouped: Dict[Tuple[int, str], Dict[str, object]] = {}

    for row in rows:
        try:
            season_id = int(row.get("season_id") or 0)
            race_created_at = clash_date_to_iso(row.get("race_created_at"))
        except (TypeError, ValueError):
            continue

        key = (season_id, race_created_at)
        race = grouped.setdefault(
            key,
            {
                "seasonId": season_id,
                "createdDate": iso_to_clash_date(race_created_at),
                "clans": [{"tag": f"#{norm_tag}", "participants": []}],
                "_history_source": "supabase",
            },
        )
        participant = {
            "tag": f"#{normalize_tag(row.get('player_tag'))}",
            "name": row.get("player_name") or normalize_tag(row.get("player_tag")),
            "role": row.get("player_role") or "",
            "fame": int(row.get("fame") or 0),
            "repairPoints": int(row.get("repair_points") or 0),
            "decksUsed": int(row.get("decks_used") or 0),
        }
        race["clans"][0]["participants"].append(participant)

    return list(grouped.values())


def load_history_races_from_env(
    clan_tag: str,
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    config = get_supabase_read_config()
    if not config:
        return [], {
            "enabled": False,
            "source": "clash_api_only",
            "message": "Supabase environment variables are not configured.",
        }

    supabase_url, api_key = config
    try:
        rows = fetch_history_rows(
            clan_tag,
            supabase_url=supabase_url,
            api_key=api_key,
        )
    except Exception:
        return [], {
            "enabled": True,
            "source": "clash_api_fallback",
            "message": "Historische Supabase-data is tijdelijk niet beschikbaar.",
        }

    races = history_rows_to_races(rows, clan_tag)
    return races, {
        "enabled": True,
        "source": "supabase_and_clash_api",
        "stored_rows": len(rows),
        "stored_weeks": len(races),
    }


def load_week_exclusions_from_env(
    clan_tag: str,
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    config = get_supabase_read_config()
    if not config:
        return [], {
            "enabled": False,
            "message": "Supabase environment variables are not configured.",
        }

    supabase_url, api_key = config
    try:
        rows = fetch_week_exclusions(
            clan_tag,
            supabase_url=supabase_url,
            api_key=api_key,
        )
    except Exception:
        return [], {
            "enabled": True,
            "message": "Supabase-weekuitzonderingen zijn tijdelijk niet beschikbaar.",
        }

    return rows, {
        "enabled": True,
        "stored_exclusions": len(rows),
    }


def build_snapshot_rows(
    clan_tag: str,
    clan_name: str,
    members: Iterable[Dict[str, object]],
    race_items: Iterable[Dict[str, object]],
) -> List[Dict[str, object]]:
    norm_tag = normalize_tag(clan_tag)
    role_map = {
        normalize_tag(member.get("tag")): str(member.get("role") or "")
        for member in members
        if normalize_tag(member.get("tag"))
    }
    captured_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    deduped: Dict[Tuple[str, str, str], Dict[str, object]] = {}

    for race in race_items:
        try:
            race_created_at = clash_date_to_iso(race.get("createdDate"))
            season_id = int(race.get("seasonId") or 0)
        except (TypeError, ValueError):
            continue

        for participant in extract_clan_participants(race, norm_tag):
            player_tag = normalize_tag(participant.get("tag"))
            if not player_tag:
                continue

            fame = max(0, int(participant.get("fame") or 0))
            repair_points = max(0, int(participant.get("repairPoints") or 0))
            decks_used = max(0, min(16, int(participant.get("decksUsed") or 0)))
            key = (norm_tag, race_created_at, player_tag)
            deduped[key] = {
                "clan_tag": norm_tag,
                "clan_name": clan_name,
                "season_id": season_id,
                "race_created_at": race_created_at,
                "player_tag": player_tag,
                "player_name": str(participant.get("name") or player_tag),
                "player_role": role_map.get(
                    player_tag,
                    str(participant.get("role") or ""),
                ),
                "fame": fame,
                "repair_points": repair_points,
                "contribution": fame + repair_points,
                "decks_used": decks_used,
                "captured_at": captured_at,
            }

    return list(deduped.values())


def _chunks(
    rows: List[Dict[str, object]],
    size: int,
) -> Iterable[List[Dict[str, object]]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def _payload_value(
    payload: Mapping[str, object],
    name: str,
    *aliases: str,
) -> Tuple[object, bool]:
    for candidate in (name, *aliases):
        if candidate in payload:
            return payload[candidate], True
    return _MISSING, False


def _required_payload_value(
    payload: Mapping[str, object],
    name: str,
    *aliases: str,
) -> object:
    value, present = _payload_value(payload, name, *aliases)
    if not present or value is None:
        raise ValueError(f"{name} is required.")
    return value


def _text_value(
    payload: Mapping[str, object],
    name: str,
    *aliases: str,
    default: object = _MISSING,
    allow_empty: bool = True,
    max_length: Optional[int] = None,
) -> str:
    value, present = _payload_value(payload, name, *aliases)
    if not present:
        if default is _MISSING:
            raise ValueError(f"{name} is required.")
        value = default
    if value is None:
        raise ValueError(f"{name} must not be null.")
    result = str(value).strip()
    if not allow_empty and not result:
        raise ValueError(f"{name} must not be empty.")
    if max_length is not None and len(result) > max_length:
        raise ValueError(f"{name} is too long.")
    return result


def _tag_value(
    payload: Mapping[str, object],
    name: str,
    *aliases: str,
) -> str:
    value = _required_payload_value(payload, name, *aliases)
    result = normalize_tag(value)
    if not result:
        raise ValueError(f"{name} must not be empty.")
    return result


def _int_value(
    value: object,
    name: str,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer.")
    if isinstance(value, float) and (
        not math.isfinite(value) or not value.is_integer()
    ):
        raise ValueError(f"{name} must be an integer.")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be an integer.") from None
    if minimum is not None and result < minimum:
        raise ValueError(f"{name} is below its minimum.")
    if maximum is not None and result > maximum:
        raise ValueError(f"{name} is above its maximum.")
    return result


def _required_int_value(
    payload: Mapping[str, object],
    name: str,
    *aliases: str,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    value = _required_payload_value(payload, name, *aliases)
    return _int_value(value, name, minimum=minimum, maximum=maximum)


def _set_optional_int(
    payload: Mapping[str, object],
    row: Dict[str, object],
    name: str,
    *aliases: str,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> None:
    value, present = _payload_value(payload, name, *aliases)
    if present:
        row[name] = (
            None
            if value is None
            else _int_value(
                value,
                name,
                minimum=minimum,
                maximum=maximum,
            )
        )


def _timestamp_value(value: object, name: str) -> str:
    try:
        return clash_date_to_iso(value)
    except Exception:
        raise ValueError(f"{name} must be a valid timestamp.") from None


def _set_optional_timestamp(
    payload: Mapping[str, object],
    row: Dict[str, object],
    name: str,
    *aliases: str,
    allow_none: bool = False,
) -> None:
    value, present = _payload_value(payload, name, *aliases)
    if not present:
        return
    if value is None and allow_none:
        row[name] = None
        return
    if value is None:
        raise ValueError(f"{name} must be a valid timestamp.")
    row[name] = _timestamp_value(value, name)


def _details_value(
    payload: Mapping[str, object],
    name: str = "details",
    *aliases: str,
) -> Dict[str, object]:
    value, present = _payload_value(payload, name, *aliases)
    if not present:
        return {}
    if value is None or not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object.")
    return dict(value)


def _map_live_snapshot(payload: Mapping[str, object]) -> Dict[str, object]:
    if not isinstance(payload, Mapping):
        raise TypeError("Live snapshot must be an object.")

    payload_version, payload_version_present = _payload_value(
        payload,
        "payload_version",
        "payloadVersion",
    )
    if not payload_version_present:
        payload_version = 1
    row: Dict[str, object] = {
        "clan_tag": _tag_value(payload, "clan_tag", "clanTag"),
        "season_id": _required_int_value(
            payload,
            "season_id",
            "seasonId",
            minimum=0,
        ),
        "period_index": _required_int_value(
            payload,
            "period_index",
            "periodIndex",
            minimum=0,
        ),
        "race_created_at": _timestamp_value(
            _required_payload_value(
                payload,
                "race_created_at",
                "raceCreatedAt",
            ),
            "race_created_at",
        ),
        "player_tag": _tag_value(payload, "player_tag", "playerTag"),
        "player_name": _text_value(
            payload,
            "player_name",
            "playerName",
            allow_empty=False,
        ),
        "player_role": _text_value(
            payload,
            "player_role",
            "playerRole",
            default="",
            max_length=32,
        ),
        "period_type": _text_value(
            payload,
            "period_type",
            "periodType",
            default="unknown",
            allow_empty=False,
            max_length=32,
        ),
        "source": _text_value(
            payload,
            "source",
            default="unknown",
            allow_empty=False,
            max_length=64,
        ),
        "payload_version": _int_value(
            payload_version,
            "payload_version",
            minimum=1,
        ),
    }
    _set_optional_int(payload, row, "section_index", "sectionIndex", minimum=0)
    _set_optional_int(payload, row, "decks_used", "decksUsed", minimum=0, maximum=16)
    _set_optional_int(payload, row, "decks_used_today", "decksUsedToday", minimum=0)
    _set_optional_int(payload, row, "fame", minimum=0)
    _set_optional_int(payload, row, "repair_points", "repairPoints", minimum=0)
    _set_optional_int(payload, row, "boat_attacks", "boatAttacks", minimum=0)
    _set_optional_int(
        payload,
        row,
        "boat_attacks_today",
        "boatAttacksToday",
        minimum=0,
    )
    _set_optional_int(payload, row, "boat_defenses", "boatDefenses", minimum=0)
    _set_optional_int(
        payload,
        row,
        "boat_defenses_today",
        "boatDefensesToday",
        minimum=0,
    )
    _set_optional_timestamp(payload, row, "captured_at", "capturedAt")
    return row


def _roster_tag_value(
    payload: Mapping[str, object],
    name: str,
    *aliases: str,
) -> str:
    """Normalize a roster identity and reject values the SQL check rejects."""

    value = _required_payload_value(payload, name, *aliases)
    candidate = _strict_roster_tag(value)
    if candidate is None:
        raise ValueError(f"{name} must be a valid tag.")
    return candidate


def _roster_optional_text(
    payload: Mapping[str, object],
    name: str,
    *aliases: str,
    default: Optional[str] = None,
    maximum: int,
) -> Optional[str]:
    value, present = _payload_value(payload, name, *aliases)
    if not present or value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text.")
    result = value.strip()
    if len(result) > maximum:
        raise ValueError(f"{name} is too long.")
    if any(ord(character) < 32 or ord(character) == 127 for character in result):
        raise ValueError(f"{name} contains invalid control characters.")
    return result or default


def _map_roster_snapshot(payload: Mapping[str, object]) -> Dict[str, object]:
    """Map one current-clan member observation to the roster table schema."""

    if not isinstance(payload, Mapping):
        raise TypeError("Roster snapshot must be an object.")

    captured_value, captured_present = _payload_value(
        payload,
        "captured_at",
        "capturedAt",
    )
    seen_value, seen_present = _payload_value(payload, "seen_at", "seenAt")
    if captured_present and captured_value is not None:
        captured_at = _timestamp_value(captured_value, "captured_at")
    elif seen_present and seen_value is not None:
        # A caller that supplies only the observation time still gets a
        # deterministic retry key; the route normally supplies both values.
        captured_at = _timestamp_value(seen_value, "seen_at")
    else:
        captured_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    if seen_present and seen_value is not None:
        seen_at = _timestamp_value(seen_value, "seen_at")
    else:
        seen_at = captured_at

    row: Dict[str, object] = {
        "clan_tag": _roster_tag_value(payload, "clan_tag", "clanTag"),
        "player_tag": _roster_tag_value(
            payload,
            "player_tag",
            "playerTag",
            "tag",
        ),
        "player_name": _roster_optional_text(
            payload,
            "player_name",
            "playerName",
            "name",
            default="unknown",
            maximum=120,
        ),
        "role": _roster_optional_text(
            payload,
            "role",
            "player_role",
            "playerRole",
            default="unknown",
            maximum=32,
        )
        or "unknown",
        "seen_at": seen_at,
        "captured_at": captured_at,
    }
    _set_optional_int(
        payload,
        row,
        "trophies",
        minimum=0,
    )
    return row


def _map_day_event(payload: Mapping[str, object]) -> Dict[str, object]:
    if not isinstance(payload, Mapping):
        raise TypeError("Day event must be an object.")

    row: Dict[str, object] = {
        "clan_tag": _tag_value(payload, "clan_tag", "clanTag"),
        "race_created_at": _timestamp_value(
            _required_payload_value(
                payload,
                "race_created_at",
                "raceCreatedAt",
            ),
            "race_created_at",
        ),
        "period_index": _required_int_value(
            payload,
            "period_index",
            "periodIndex",
            minimum=0,
        ),
        "player_tag": _tag_value(payload, "player_tag", "playerTag"),
        "event_type": _text_value(
            payload,
            "event_type",
            "eventType",
            allow_empty=False,
            max_length=64,
        ),
        "confidence": _text_value(
            payload,
            "confidence",
            default="unknown",
            allow_empty=False,
        ),
        "details": _details_value(payload),
    }
    if row["confidence"] not in {"unknown", "low", "medium", "high"}:
        raise ValueError("confidence has an unsupported value.")
    _set_optional_int(
        payload,
        row,
        "observed_decks_used_today",
        "observedDecksUsedToday",
        minimum=0,
    )
    _set_optional_timestamp(payload, row, "observed_at", "observedAt")
    _set_optional_timestamp(payload, row, "created_at", "createdAt")
    return row


def _map_notification_log(payload: Mapping[str, object]) -> Dict[str, object]:
    if not isinstance(payload, Mapping):
        raise TypeError("Notification log entry must be an object.")

    row: Dict[str, object] = {
        "event_key": _text_value(
            payload,
            "event_key",
            "eventKey",
            allow_empty=False,
            max_length=256,
        ),
        "channel": _text_value(
            payload,
            "channel",
            allow_empty=False,
            max_length=64,
        ),
        "status": _text_value(
            payload,
            "status",
            default="pending",
            allow_empty=False,
            max_length=32,
        ),
        "details": _details_value(payload),
    }
    _set_optional_int(
        payload,
        row,
        "response_code",
        "responseCode",
        minimum=100,
        maximum=599,
    )
    _set_optional_timestamp(payload, row, "sent_at", "sentAt", allow_none=True)
    return row


def _notification_identity_value(value: object, name: str, max_length: int) -> str:
    return _text_value(
        {name: value},
        name,
        allow_empty=False,
        max_length=max_length,
    )


def _clean_server_url(value: object) -> str:
    url = str(value or "").strip().rstrip("/")
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        raise SupabaseStorageError("configuration_error")
    return url


def _resolve_raw_storage_config(
    *,
    supabase_url: Optional[str],
    api_key: Optional[str],
    supabase_api_key: Optional[str],
) -> Tuple[str, str]:
    explicit_keys = [
        str(value).strip()
        for value in (api_key, supabase_api_key)
        if value is not None and str(value).strip()
    ]
    if len(explicit_keys) > 1 and explicit_keys[0] != explicit_keys[1]:
        raise SupabaseStorageError("configuration_error")

    if explicit_keys:
        selected_key = explicit_keys[0]
        if _is_public_supabase_key(selected_key):
            raise SupabaseStorageError("server_key_required")
        configured_url = supabase_url or os.environ.get("SUPABASE_URL", "")
        configured_url = configured_url.strip() or DEFAULT_SUPABASE_URL
        return _clean_server_url(configured_url), selected_key

    configured = get_supabase_server_config()
    if not configured:
        raise SupabaseStorageError("configuration_error")
    configured_url, selected_key = configured
    if _is_public_supabase_key(selected_key):
        raise SupabaseStorageError("server_key_required")
    return _clean_server_url(configured_url), selected_key


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
        or not 0 <= max_retries <= 3
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


def _validate_batch_size(batch_size: int) -> None:
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
        raise ValueError("batch_size must be a positive integer.")


def _validate_stale_after(stale_after_seconds: Optional[float]) -> None:
    if stale_after_seconds is None:
        return
    if (
        isinstance(stale_after_seconds, bool)
        or not isinstance(stale_after_seconds, (int, float))
        or not math.isfinite(float(stale_after_seconds))
        or stale_after_seconds < 0
    ):
        raise ValueError(
            "stale_after_seconds must be a finite non-negative number or None."
        )


def _retry_delay(
    response: Any,
    *,
    attempt: int,
    backoff_factor: float,
    max_backoff: float,
) -> float:
    retry_after = None
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            raw_retry_after = headers.get("Retry-After")
            if raw_retry_after is not None:
                retry_after = float(raw_retry_after)
        except (TypeError, ValueError, OverflowError):
            retry_after = None
    if retry_after is not None and math.isfinite(retry_after):
        return min(max_backoff, max(0.0, retry_after))
    return min(max_backoff, backoff_factor * (2 ** max(0, attempt - 1)))


def _storage_request(
    method: str,
    endpoint: str,
    *,
    table: str,
    headers: Dict[str, str],
    params: Optional[Dict[str, str]] = None,
    payload: Optional[List[Dict[str, object]]] = None,
    timeout: float,
    max_retries: int,
    backoff_factor: float,
    max_backoff: float,
    sleep: Optional[Callable[[float], None]],
) -> Tuple[Any, int]:
    sleep_fn = sleep or time.sleep
    request_method = method.upper()
    for attempt in range(1, max_retries + 2):
        try:
            if request_method == "GET":
                response = requests.get(
                    endpoint,
                    params=params,
                    headers=headers,
                    timeout=timeout,
                )
            elif request_method == "POST":
                response = requests.post(
                    endpoint,
                    json=payload,
                    headers=headers,
                    timeout=timeout,
                )
            else:
                raise SupabaseStorageError("configuration_error", attempts=attempt)
        except requests.exceptions.RequestException as exc:
            code = (
                "timeout"
                if isinstance(exc, requests.exceptions.Timeout)
                else "transport_error"
            )
            if attempt <= max_retries:
                delay = min(
                    max_backoff,
                    backoff_factor * (2 ** max(0, attempt - 1)),
                )
                if delay > 0:
                    sleep_fn(delay)
                continue
            raise SupabaseStorageError(code, attempts=attempt) from None

        try:
            status_code = int(response.status_code)
        except (TypeError, ValueError, AttributeError):
            raise SupabaseStorageError(
                "invalid_response",
                attempts=attempt,
            ) from None

        if 200 <= status_code < 300:
            return response, attempt

        retryable = (
            status_code in _RETRYABLE_STORAGE_STATUSES
            or 500 <= status_code <= 599
        )
        if retryable and attempt <= max_retries:
            delay = _retry_delay(
                response,
                attempt=attempt,
                backoff_factor=backoff_factor,
                max_backoff=max_backoff,
            )
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
        raise SupabaseStorageError(
            code,
            status_code=status_code,
            attempts=attempt,
        ) from None

    raise SupabaseStorageError("transport_error", attempts=max_retries + 1)


def _storage_error_result(
    table: str,
    error: SupabaseStorageError,
    *,
    row_key: Optional[str] = None,
    rows_written: int = 0,
    batches: int = 0,
    attempts: Optional[int] = None,
) -> Dict[str, object]:
    result: Dict[str, object] = {
        "ok": False,
        "status": STORAGE_STATUS_ERROR,
        "data_status": STORAGE_STATUS_ERROR,
        "table": table,
        "error": error.code,
        "message": _safe_storage_message(error.code),
        "rows_written": rows_written,
        "rows_upserted": rows_written,
        "batches": batches,
        "attempts": error.attempts if attempts is None else attempts,
    }
    if error.status_code is not None:
        result["status_code"] = error.status_code
    if row_key:
        result[row_key] = None
        result["stale"] = False
        result["is_stale"] = False
    return result


def _raw_endpoint(supabase_url: str, table: str, conflict_columns: Tuple[str, ...]) -> str:
    return (
        f"{supabase_url}/rest/v1/{table}?on_conflict="
        + ",".join(conflict_columns)
    )


def _write_raw_rows(
    rows: List[Dict[str, object]],
    *,
    table: str,
    conflict_columns: Tuple[str, ...],
    supabase_url: Optional[str],
    api_key: Optional[str],
    supabase_api_key: Optional[str],
    batch_size: int,
    timeout: float,
    max_retries: int,
    backoff_factor: float,
    max_backoff: float,
    sleep: Optional[Callable[[float], None]],
) -> Dict[str, object]:
    _validate_batch_size(batch_size)
    _validate_request_options(
        timeout=timeout,
        max_retries=max_retries,
        backoff_factor=backoff_factor,
        max_backoff=max_backoff,
    )
    if not rows:
        return {
            "ok": True,
            "status": STORAGE_STATUS_EMPTY,
            "data_status": STORAGE_STATUS_EMPTY,
            "table": table,
            "rows_written": 0,
            "rows_upserted": 0,
            "batches": 0,
            "attempts": 0,
        }

    written = 0
    batches = 0
    attempts = 0
    try:
        json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        configured_url, configured_key = _resolve_raw_storage_config(
            supabase_url=supabase_url,
            api_key=api_key,
            supabase_api_key=supabase_api_key,
        )
        endpoint = _raw_endpoint(configured_url, table, conflict_columns)
        headers = _supabase_headers(configured_key, write=True)
        for batch in _chunks(rows, batch_size):
            _, batch_attempts = _storage_request(
                "POST",
                endpoint,
                table=table,
                headers=headers,
                payload=batch,
                timeout=timeout,
                max_retries=max_retries,
                backoff_factor=backoff_factor,
                max_backoff=max_backoff,
                sleep=sleep,
            )
            attempts += batch_attempts
            written += len(batch)
            batches += 1
    except SupabaseStorageError as error:
        return _storage_error_result(
            table,
            error,
            rows_written=written,
            batches=batches,
            attempts=attempts + error.attempts,
        )
    except (TypeError, ValueError, OverflowError):
        return _storage_error_result(
            table,
            SupabaseStorageError("invalid_payload"),
            rows_written=written,
            batches=batches,
            attempts=attempts,
        )

    return {
        "ok": True,
        "status": STORAGE_STATUS_OK,
        "data_status": STORAGE_STATUS_OK,
        "table": table,
        "rows_written": written,
        "rows_upserted": written,
        "batches": batches,
        "attempts": attempts,
    }


def write_live_snapshots(
    snapshots: Iterable[Mapping[str, object]],
    *,
    supabase_url: Optional[str] = None,
    api_key: Optional[str] = None,
    supabase_api_key: Optional[str] = None,
    batch_size: int = DEFAULT_WRITE_BATCH_SIZE,
    timeout: float = DEFAULT_RAW_STORAGE_TIMEOUT,
    max_retries: int = DEFAULT_RAW_STORAGE_MAX_RETRIES,
    backoff_factor: float = DEFAULT_RAW_STORAGE_BACKOFF_SECONDS,
    max_backoff: float = DEFAULT_RAW_STORAGE_MAX_BACKOFF_SECONDS,
    sleep: Optional[Callable[[float], None]] = None,
) -> Dict[str, object]:
    """Upsert live snapshot rows using the T05 natural key.

    The input may use T05 snake_case names or the corresponding Clash-style
    camelCase names.  Only T05 writable columns are sent.  The result has
    ``status`` ``ok``, ``empty``, or ``error`` and never substitutes metric
    defaults for unavailable data.
    """

    rows = [_map_live_snapshot(snapshot) for snapshot in snapshots]
    return _write_raw_rows(
        rows,
        table=LIVE_SNAPSHOT_TABLE,
        conflict_columns=LIVE_SNAPSHOT_CONFLICT_COLUMNS,
        supabase_url=supabase_url,
        api_key=api_key,
        supabase_api_key=supabase_api_key,
        batch_size=batch_size,
        timeout=timeout,
        max_retries=max_retries,
        backoff_factor=backoff_factor,
        max_backoff=max_backoff,
        sleep=sleep,
    )


def write_live_snapshot(
    snapshot: Mapping[str, object],
    *,
    supabase_url: Optional[str] = None,
    api_key: Optional[str] = None,
    supabase_api_key: Optional[str] = None,
    batch_size: int = DEFAULT_WRITE_BATCH_SIZE,
    timeout: float = DEFAULT_RAW_STORAGE_TIMEOUT,
    max_retries: int = DEFAULT_RAW_STORAGE_MAX_RETRIES,
    backoff_factor: float = DEFAULT_RAW_STORAGE_BACKOFF_SECONDS,
    max_backoff: float = DEFAULT_RAW_STORAGE_MAX_BACKOFF_SECONDS,
    sleep: Optional[Callable[[float], None]] = None,
) -> Dict[str, object]:
    """Upsert one live player snapshot and return a safe storage result."""

    return write_live_snapshots(
        [snapshot],
        supabase_url=supabase_url,
        api_key=api_key,
        supabase_api_key=supabase_api_key,
        batch_size=batch_size,
        timeout=timeout,
        max_retries=max_retries,
        backoff_factor=backoff_factor,
        max_backoff=max_backoff,
        sleep=sleep,
    )


def write_roster_snapshots(
    snapshots: Iterable[Mapping[str, object]],
    *,
    supabase_url: Optional[str] = None,
    api_key: Optional[str] = None,
    supabase_api_key: Optional[str] = None,
    batch_size: int = DEFAULT_WRITE_BATCH_SIZE,
    timeout: float = DEFAULT_RAW_STORAGE_TIMEOUT,
    max_retries: int = DEFAULT_RAW_STORAGE_MAX_RETRIES,
    backoff_factor: float = DEFAULT_RAW_STORAGE_BACKOFF_SECONDS,
    max_backoff: float = DEFAULT_RAW_STORAGE_MAX_BACKOFF_SECONDS,
    sleep: Optional[Callable[[float], None]] = None,
) -> Dict[str, object]:
    """Upsert normalized roster observations with an idempotent natural key.

    ``captured_at`` is part of the identity on purpose: a retry of the same
    roster capture updates one row, while a later capture creates the next
    observation.  Rows are deduplicated before batching so repeated player
    tags in one upstream response cannot create duplicate observations.
    """

    deduped: Dict[Tuple[str, str, str], Dict[str, object]] = {}
    for snapshot in snapshots:
        row = _map_roster_snapshot(snapshot)
        if "trophies" not in row:
            row["trophies"] = None
        key = (
            str(row["clan_tag"]),
            str(row["player_tag"]),
            str(row["captured_at"]),
        )
        deduped[key] = row

    return _write_raw_rows(
        list(deduped.values()),
        table=ROSTER_SNAPSHOT_TABLE,
        conflict_columns=ROSTER_SNAPSHOT_CONFLICT_COLUMNS,
        supabase_url=supabase_url,
        api_key=api_key,
        supabase_api_key=supabase_api_key,
        batch_size=batch_size,
        timeout=timeout,
        max_retries=max_retries,
        backoff_factor=backoff_factor,
        max_backoff=max_backoff,
        sleep=sleep,
    )


def write_roster_snapshot(
    snapshot: Mapping[str, object],
    *,
    supabase_url: Optional[str] = None,
    api_key: Optional[str] = None,
    supabase_api_key: Optional[str] = None,
    batch_size: int = DEFAULT_WRITE_BATCH_SIZE,
    timeout: float = DEFAULT_RAW_STORAGE_TIMEOUT,
    max_retries: int = DEFAULT_RAW_STORAGE_MAX_RETRIES,
    backoff_factor: float = DEFAULT_RAW_STORAGE_BACKOFF_SECONDS,
    max_backoff: float = DEFAULT_RAW_STORAGE_MAX_BACKOFF_SECONDS,
    sleep: Optional[Callable[[float], None]] = None,
) -> Dict[str, object]:
    """Upsert one roster observation and return a safe storage result."""

    return write_roster_snapshots(
        [snapshot],
        supabase_url=supabase_url,
        api_key=api_key,
        supabase_api_key=supabase_api_key,
        batch_size=batch_size,
        timeout=timeout,
        max_retries=max_retries,
        backoff_factor=backoff_factor,
        max_backoff=max_backoff,
        sleep=sleep,
    )


def _read_raw_row(
    *,
    table: str,
    result_key: str,
    select_columns: str,
    params: Dict[str, str],
    timestamp_field: str,
    stale_after_seconds: Optional[float],
    supabase_url: Optional[str],
    api_key: Optional[str],
    supabase_api_key: Optional[str],
    timeout: float,
    max_retries: int,
    backoff_factor: float,
    max_backoff: float,
    sleep: Optional[Callable[[float], None]],
) -> Dict[str, object]:
    _validate_stale_after(stale_after_seconds)
    _validate_request_options(
        timeout=timeout,
        max_retries=max_retries,
        backoff_factor=backoff_factor,
        max_backoff=max_backoff,
    )
    attempts = 0
    try:
        configured_url, configured_key = _resolve_raw_storage_config(
            supabase_url=supabase_url,
            api_key=api_key,
            supabase_api_key=supabase_api_key,
        )
        endpoint = f"{configured_url}/rest/v1/{table}"
        response, attempts = _storage_request(
            "GET",
            endpoint,
            table=table,
            headers=_supabase_headers(configured_key),
            params={"select": select_columns, **params},
            timeout=timeout,
            max_retries=max_retries,
            backoff_factor=backoff_factor,
            max_backoff=max_backoff,
            sleep=sleep,
        )
        content = getattr(response, "content", b"")
        raw_rows = response.json() if content else []
        if not isinstance(raw_rows, list):
            raise SupabaseStorageError("invalid_response", attempts=attempts)
        if not raw_rows:
            return {
                "ok": True,
                "status": STORAGE_STATUS_EMPTY,
                "data_status": STORAGE_STATUS_EMPTY,
                "table": table,
                result_key: None,
                "stale": False,
                "is_stale": False,
                "attempts": attempts,
            }
        if not isinstance(raw_rows[0], Mapping):
            raise SupabaseStorageError("invalid_response", attempts=attempts)
        row = dict(raw_rows[0])
        stale = False
        if stale_after_seconds is not None:
            timestamp = row.get(timestamp_field)
            if timestamp is None:
                raise SupabaseStorageError("invalid_response", attempts=attempts)
            try:
                observed_at = clash_date_to_datetime(timestamp)
            except Exception:
                raise SupabaseStorageError(
                    "invalid_response",
                    attempts=attempts,
                ) from None
            age_seconds = max(
                0.0,
                (datetime.now(timezone.utc) - observed_at).total_seconds(),
            )
            stale = age_seconds > float(stale_after_seconds)
        result: Dict[str, object] = {
            "ok": True,
            "status": STORAGE_STATUS_STALE if stale else STORAGE_STATUS_FRESH,
            "data_status": (
                STORAGE_STATUS_STALE if stale else STORAGE_STATUS_FRESH
            ),
            "table": table,
            result_key: row,
            "stale": stale,
            "is_stale": stale,
            "attempts": attempts,
        }
        if stale:
            result["stale_reason"] = "age"
        return result
    except SupabaseStorageError as error:
        return _storage_error_result(
            table,
            error,
            row_key=result_key,
            attempts=attempts + error.attempts,
        )
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return _storage_error_result(
            table,
            SupabaseStorageError("invalid_response"),
            row_key=result_key,
            attempts=attempts,
        )


def _normalize_roster_read_row(
    raw: Mapping[str, object],
    clan_tag: str,
    player_tag: Optional[str],
) -> Optional[Dict[str, object]]:
    """Validate and project a row returned by the roster table.

    The second clan/player filter is intentional defense in depth: a malformed
    or misconfigured PostgREST response must not cross the requested clan
    boundary into a public lifecycle response.
    """

    try:
        row_clan = _strict_roster_tag(raw.get("clan_tag", raw.get("clanTag")))
        row_player = _strict_roster_tag(raw.get("player_tag", raw.get("playerTag")))
    except Exception:
        return None
    if row_clan != clan_tag or not row_player:
        return None
    if player_tag and row_player != player_tag:
        return None

    captured_value = raw.get("captured_at", raw.get("capturedAt"))
    seen_value = raw.get("seen_at", raw.get("seenAt"))
    try:
        captured_at = _timestamp_value(captured_value, "captured_at")
        seen_at = _timestamp_value(
            seen_value if seen_value is not None else captured_at,
            "seen_at",
        )
    except ValueError:
        return None

    player_name = raw.get("player_name", raw.get("playerName"))
    if player_name is not None and not isinstance(player_name, str):
        player_name = str(player_name)
    if isinstance(player_name, str):
        player_name = player_name.strip() or "unknown"
    if player_name is None:
        player_name = "unknown"

    role = raw.get("role", raw.get("player_role", raw.get("playerRole")))
    if role is None:
        role = "unknown"
    elif not isinstance(role, str):
        role = str(role)
    role = role.strip() or "unknown"

    trophies = raw.get("trophies")
    if trophies is not None:
        try:
            trophies = _int_value(trophies, "trophies", minimum=0)
        except ValueError:
            trophies = None

    return {
        "clan_tag": row_clan,
        "player_tag": row_player,
        "player_name": player_name,
        "role": role,
        "trophies": trophies,
        "seen_at": seen_at,
        "captured_at": captured_at,
    }


def read_roster_snapshots(
    clan_tag: str,
    *,
    player_tag: Optional[str] = None,
    max_rows: int = DEFAULT_ROSTER_MAX_ROWS,
    stale_after_seconds: Optional[float] = None,
    supabase_url: Optional[str] = None,
    api_key: Optional[str] = None,
    supabase_api_key: Optional[str] = None,
    timeout: float = DEFAULT_RAW_STORAGE_TIMEOUT,
    max_retries: int = DEFAULT_RAW_STORAGE_MAX_RETRIES,
    backoff_factor: float = DEFAULT_RAW_STORAGE_BACKOFF_SECONDS,
    max_backoff: float = DEFAULT_RAW_STORAGE_MAX_BACKOFF_SECONDS,
    sleep: Optional[Callable[[float], None]] = None,
) -> Dict[str, object]:
    """Read a bounded, server-side roster history with explicit data status."""

    normalized_clan = _strict_roster_tag(clan_tag)
    if normalized_clan is None:
        raise ValueError("clan_tag is required and must be a valid tag.")
    normalized_player = None
    if player_tag is not None:
        normalized_player = _strict_roster_tag(player_tag)
        if normalized_player is None:
            raise ValueError("player_tag must be a valid tag.")
    if (
        isinstance(max_rows, bool)
        or not isinstance(max_rows, int)
        or not 1 <= max_rows <= DEFAULT_ROSTER_MAX_ROWS
    ):
        raise ValueError(
            f"max_rows must be between 1 and {DEFAULT_ROSTER_MAX_ROWS}."
        )

    _validate_stale_after(stale_after_seconds)
    _validate_request_options(
        timeout=timeout,
        max_retries=max_retries,
        backoff_factor=backoff_factor,
        max_backoff=max_backoff,
    )

    params = {
        "select": ROSTER_SNAPSHOT_SELECT_COLUMNS,
        "clan_tag": f"eq.{normalized_clan}",
        "order": "captured_at.asc,player_tag.asc",
        "limit": str(max_rows),
    }
    if normalized_player:
        params["player_tag"] = f"eq.{normalized_player}"

    attempts = 0
    try:
        configured_url, configured_key = _resolve_raw_storage_config(
            supabase_url=supabase_url,
            api_key=api_key,
            supabase_api_key=supabase_api_key,
        )
        endpoint = f"{configured_url}/rest/v1/{ROSTER_SNAPSHOT_TABLE}"
        response, attempts = _storage_request(
            "GET",
            endpoint,
            table=ROSTER_SNAPSHOT_TABLE,
            headers=_supabase_headers(configured_key),
            params=params,
            timeout=timeout,
            max_retries=max_retries,
            backoff_factor=backoff_factor,
            max_backoff=max_backoff,
            sleep=sleep,
        )
        content = getattr(response, "content", b"")
        raw_rows = response.json() if content else []
        if not isinstance(raw_rows, list):
            raise SupabaseStorageError("invalid_response", attempts=attempts)
        truncated = False
        content_range = getattr(response, "headers", {}).get("Content-Range")
        if isinstance(content_range, str):
            match = re.search(r"/(\d+)\s*$", content_range.strip())
            if match:
                try:
                    truncated = int(match.group(1)) > max_rows
                except (TypeError, ValueError, OverflowError):
                    truncated = False

        rows: List[Dict[str, object]] = []
        invalid_rows = 0
        for raw in raw_rows:
            if not isinstance(raw, Mapping):
                invalid_rows += 1
                continue
            # Rows outside the requested scope are ignored rather than
            # treated as partial roster data.  PostgREST already filters this
            # server-side, but the second check prevents cross-clan leakage
            # if an upstream response is broader than requested.
            raw_clan = _strict_roster_tag(
                raw.get("clan_tag", raw.get("clanTag"))
            )
            raw_player = _strict_roster_tag(
                raw.get("player_tag", raw.get("playerTag"))
            )
            if raw_clan != normalized_clan:
                continue
            if normalized_player and raw_player != normalized_player:
                continue
            row = _normalize_roster_read_row(
                raw,
                normalized_clan,
                normalized_player,
            )
            if row is None:
                invalid_rows += 1
                continue
            rows.append(row)

        if not rows:
            status = STORAGE_STATUS_PARTIAL if invalid_rows else STORAGE_STATUS_EMPTY
            result: Dict[str, object] = {
                "ok": True,
                "status": status,
                "data_status": status,
                "table": ROSTER_SNAPSHOT_TABLE,
                "snapshots": [],
                "rows": [],
                "count": 0,
                "invalid_rows": invalid_rows,
                "truncated": truncated,
                "stale": False,
                "is_stale": False,
                "attempts": attempts,
            }
            return result

        stale = False
        if stale_after_seconds is not None:
            try:
                latest = max(
                    _timestamp_value(row["captured_at"], "captured_at")
                    for row in rows
                )
                latest_dt = clash_date_to_datetime(latest)
                age_seconds = max(
                    0.0,
                    (datetime.now(timezone.utc) - latest_dt).total_seconds(),
                )
                stale = age_seconds > float(stale_after_seconds)
            except (TypeError, ValueError, OverflowError):
                raise SupabaseStorageError(
                    "invalid_response",
                    attempts=attempts,
                ) from None

        status = (
            STORAGE_STATUS_PARTIAL
            if invalid_rows or truncated
            else STORAGE_STATUS_STALE
            if stale
            else STORAGE_STATUS_FRESH
        )
        result = {
            "ok": True,
            "status": status,
            "data_status": status,
            "table": ROSTER_SNAPSHOT_TABLE,
            "snapshots": rows,
            "rows": rows,
            "count": len(rows),
            "invalid_rows": invalid_rows,
            "truncated": truncated,
            "stale": stale,
            "is_stale": stale,
            "attempts": attempts,
        }
        if stale:
            result["stale_reason"] = "age"
        return result
    except SupabaseStorageError as error:
        result = _storage_error_result(
            ROSTER_SNAPSHOT_TABLE,
            error,
            row_key="snapshots",
            attempts=attempts + error.attempts,
        )
        result["rows"] = None
        result["count"] = 0
        return result
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        result = _storage_error_result(
            ROSTER_SNAPSHOT_TABLE,
            SupabaseStorageError("invalid_response"),
            row_key="snapshots",
            attempts=attempts,
        )
        result["rows"] = None
        result["count"] = 0
        return result


def read_previous_player_snapshot(
    clan_tag: str,
    race_created_at: object,
    period_index: object,
    player_tag: str,
    *,
    before_captured_at: Optional[object] = None,
    stale_after_seconds: Optional[float] = None,
    supabase_url: Optional[str] = None,
    api_key: Optional[str] = None,
    supabase_api_key: Optional[str] = None,
    timeout: float = DEFAULT_RAW_STORAGE_TIMEOUT,
    max_retries: int = DEFAULT_RAW_STORAGE_MAX_RETRIES,
    backoff_factor: float = DEFAULT_RAW_STORAGE_BACKOFF_SECONDS,
    max_backoff: float = DEFAULT_RAW_STORAGE_MAX_BACKOFF_SECONDS,
    sleep: Optional[Callable[[float], None]] = None,
) -> Dict[str, object]:
    """Read the latest earlier player snapshot without zero-filling missing data."""

    normalized_clan_tag = normalize_tag(clan_tag)
    normalized_player_tag = normalize_tag(player_tag)
    if not normalized_clan_tag or not normalized_player_tag:
        raise ValueError("clan_tag and player_tag are required.")
    normalized_race_created_at = _timestamp_value(
        race_created_at,
        "race_created_at",
    )
    normalized_period_index = _int_value(
        period_index,
        "period_index",
        minimum=0,
    )
    params = {
        "clan_tag": f"eq.{normalized_clan_tag}",
        "race_created_at": f"eq.{normalized_race_created_at}",
        "period_index": f"eq.{normalized_period_index}",
        "player_tag": f"eq.{normalized_player_tag}",
        "order": "captured_at.desc",
        "limit": "1",
    }
    if before_captured_at is not None:
        params["captured_at"] = (
            "lt." + _timestamp_value(before_captured_at, "before_captured_at")
        )
    return _read_raw_row(
        table=LIVE_SNAPSHOT_TABLE,
        result_key="snapshot",
        select_columns=LIVE_SNAPSHOT_SELECT_COLUMNS,
        params=params,
        timestamp_field="captured_at",
        stale_after_seconds=stale_after_seconds,
        supabase_url=supabase_url,
        api_key=api_key,
        supabase_api_key=supabase_api_key,
        timeout=timeout,
        max_retries=max_retries,
        backoff_factor=backoff_factor,
        max_backoff=max_backoff,
        sleep=sleep,
    )


def write_day_events(
    events: Iterable[Mapping[str, object]],
    *,
    supabase_url: Optional[str] = None,
    api_key: Optional[str] = None,
    supabase_api_key: Optional[str] = None,
    batch_size: int = DEFAULT_WRITE_BATCH_SIZE,
    timeout: float = DEFAULT_RAW_STORAGE_TIMEOUT,
    max_retries: int = DEFAULT_RAW_STORAGE_MAX_RETRIES,
    backoff_factor: float = DEFAULT_RAW_STORAGE_BACKOFF_SECONDS,
    max_backoff: float = DEFAULT_RAW_STORAGE_MAX_BACKOFF_SECONDS,
    sleep: Optional[Callable[[float], None]] = None,
) -> Dict[str, object]:
    """Upsert day events using the T05 event idempotency key."""

    rows = [_map_day_event(event) for event in events]
    return _write_raw_rows(
        rows,
        table=DAY_EVENTS_TABLE,
        conflict_columns=DAY_EVENT_CONFLICT_COLUMNS,
        supabase_url=supabase_url,
        api_key=api_key,
        supabase_api_key=supabase_api_key,
        batch_size=batch_size,
        timeout=timeout,
        max_retries=max_retries,
        backoff_factor=backoff_factor,
        max_backoff=max_backoff,
        sleep=sleep,
    )


def write_day_event(
    event: Mapping[str, object],
    *,
    supabase_url: Optional[str] = None,
    api_key: Optional[str] = None,
    supabase_api_key: Optional[str] = None,
    batch_size: int = DEFAULT_WRITE_BATCH_SIZE,
    timeout: float = DEFAULT_RAW_STORAGE_TIMEOUT,
    max_retries: int = DEFAULT_RAW_STORAGE_MAX_RETRIES,
    backoff_factor: float = DEFAULT_RAW_STORAGE_BACKOFF_SECONDS,
    max_backoff: float = DEFAULT_RAW_STORAGE_MAX_BACKOFF_SECONDS,
    sleep: Optional[Callable[[float], None]] = None,
) -> Dict[str, object]:
    """Upsert one day event and return a safe storage result."""

    return write_day_events(
        [event],
        supabase_url=supabase_url,
        api_key=api_key,
        supabase_api_key=supabase_api_key,
        batch_size=batch_size,
        timeout=timeout,
        max_retries=max_retries,
        backoff_factor=backoff_factor,
        max_backoff=max_backoff,
        sleep=sleep,
    )


def read_day_event(
    clan_tag: str,
    race_created_at: object,
    period_index: object,
    player_tag: str,
    event_type: str,
    *,
    stale_after_seconds: Optional[float] = None,
    supabase_url: Optional[str] = None,
    api_key: Optional[str] = None,
    supabase_api_key: Optional[str] = None,
    timeout: float = DEFAULT_RAW_STORAGE_TIMEOUT,
    max_retries: int = DEFAULT_RAW_STORAGE_MAX_RETRIES,
    backoff_factor: float = DEFAULT_RAW_STORAGE_BACKOFF_SECONDS,
    max_backoff: float = DEFAULT_RAW_STORAGE_MAX_BACKOFF_SECONDS,
    sleep: Optional[Callable[[float], None]] = None,
) -> Dict[str, object]:
    """Read one idempotent day event and expose fresh/stale/error explicitly."""

    normalized_clan_tag = normalize_tag(clan_tag)
    normalized_player_tag = normalize_tag(player_tag)
    if not normalized_clan_tag or not normalized_player_tag:
        raise ValueError("clan_tag and player_tag are required.")
    normalized_race_created_at = _timestamp_value(
        race_created_at,
        "race_created_at",
    )
    normalized_period_index = _int_value(
        period_index,
        "period_index",
        minimum=0,
    )
    normalized_event_type = str(event_type or "").strip()
    if not normalized_event_type:
        raise ValueError("event_type must not be empty.")
    if len(normalized_event_type) > 64:
        raise ValueError("event_type is too long.")
    params = {
        "clan_tag": f"eq.{normalized_clan_tag}",
        "race_created_at": f"eq.{normalized_race_created_at}",
        "period_index": f"eq.{normalized_period_index}",
        "player_tag": f"eq.{normalized_player_tag}",
        "event_type": f"eq.{normalized_event_type}",
        "order": "observed_at.desc",
        "limit": "1",
    }
    return _read_raw_row(
        table=DAY_EVENTS_TABLE,
        result_key="event",
        select_columns=DAY_EVENT_SELECT_COLUMNS,
        params=params,
        timestamp_field="observed_at",
        stale_after_seconds=stale_after_seconds,
        supabase_url=supabase_url,
        api_key=api_key,
        supabase_api_key=supabase_api_key,
        timeout=timeout,
        max_retries=max_retries,
        backoff_factor=backoff_factor,
        max_backoff=max_backoff,
        sleep=sleep,
    )


def read_notification_log(
    event_key: object,
    channel: object,
    *,
    supabase_url: Optional[str] = None,
    api_key: Optional[str] = None,
    supabase_api_key: Optional[str] = None,
    timeout: float = DEFAULT_RAW_STORAGE_TIMEOUT,
    max_retries: int = DEFAULT_RAW_STORAGE_MAX_RETRIES,
    backoff_factor: float = DEFAULT_RAW_STORAGE_BACKOFF_SECONDS,
    max_backoff: float = DEFAULT_RAW_STORAGE_MAX_BACKOFF_SECONDS,
    sleep: Optional[Callable[[float], None]] = None,
) -> Dict[str, object]:
    """Read one notification idempotency row without exposing raw secrets."""

    normalized_event_key = _notification_identity_value(event_key, "event_key", 256)
    normalized_channel = _notification_identity_value(channel, "channel", 64)
    params = {
        "event_key": f"eq.{normalized_event_key}",
        "channel": f"eq.{normalized_channel}",
        "order": "sent_at.desc",
        "limit": "1",
    }
    return _read_raw_row(
        table=NOTIFICATION_LOG_TABLE,
        result_key="notification_log",
        select_columns=NOTIFICATION_LOG_SELECT_COLUMNS,
        params=params,
        timestamp_field="sent_at",
        stale_after_seconds=None,
        supabase_url=supabase_url,
        api_key=api_key,
        supabase_api_key=supabase_api_key,
        timeout=timeout,
        max_retries=max_retries,
        backoff_factor=backoff_factor,
        max_backoff=max_backoff,
        sleep=sleep,
    )


def claim_notification_log(
    entry: Mapping[str, object],
    *,
    supabase_url: Optional[str] = None,
    api_key: Optional[str] = None,
    supabase_api_key: Optional[str] = None,
    timeout: float = DEFAULT_RAW_STORAGE_TIMEOUT,
    max_retries: int = DEFAULT_RAW_STORAGE_MAX_RETRIES,
    backoff_factor: float = DEFAULT_RAW_STORAGE_BACKOFF_SECONDS,
    max_backoff: float = DEFAULT_RAW_STORAGE_MAX_BACKOFF_SECONDS,
    sleep: Optional[Callable[[float], None]] = None,
) -> Dict[str, object]:
    """Atomically claim an event/channel pair before an external send.

    ``resolution=ignore-duplicates`` makes the unique T05 key the durable
    single-send gate.  The response body is used only to distinguish a newly
    inserted row from an already claimed row; it is never returned to callers.
    """

    row = _map_notification_log({**dict(entry), "status": "pending"})
    _validate_batch_size(1)
    _validate_request_options(
        timeout=timeout,
        max_retries=max_retries,
        backoff_factor=backoff_factor,
        max_backoff=max_backoff,
    )
    attempts = 0
    try:
        json.dumps([row], ensure_ascii=False, separators=(",", ":"))
        configured_url, configured_key = _resolve_raw_storage_config(
            supabase_url=supabase_url,
            api_key=api_key,
            supabase_api_key=supabase_api_key,
        )
        endpoint = _raw_endpoint(
            configured_url,
            NOTIFICATION_LOG_TABLE,
            NOTIFICATION_LOG_CONFLICT_COLUMNS,
        )
        headers = _supabase_headers(configured_key, write=True)
        headers["Prefer"] = "resolution=ignore-duplicates,return=representation"
        response, attempts = _storage_request(
            "POST",
            endpoint,
            table=NOTIFICATION_LOG_TABLE,
            headers=headers,
            payload=[row],
            timeout=timeout,
            max_retries=max_retries,
            backoff_factor=backoff_factor,
            max_backoff=max_backoff,
            sleep=sleep,
        )
        content = getattr(response, "content", b"")
        raw_rows = response.json() if content else []
        if not isinstance(raw_rows, list):
            raise SupabaseStorageError("invalid_response", attempts=attempts)
        if raw_rows and not isinstance(raw_rows[0], Mapping):
            raise SupabaseStorageError("invalid_response", attempts=attempts)
        claimed = bool(raw_rows)
        return {
            "ok": True,
            "status": "claimed" if claimed else "exists",
            "data_status": STORAGE_STATUS_OK if claimed else STORAGE_STATUS_FRESH,
            "table": NOTIFICATION_LOG_TABLE,
            "claimed": claimed,
            "rows_written": 1 if claimed else 0,
            "rows_upserted": 1 if claimed else 0,
            "attempts": attempts,
        }
    except SupabaseStorageError as error:
        result = _storage_error_result(
            NOTIFICATION_LOG_TABLE,
            error,
            attempts=attempts + error.attempts,
        )
        result["claimed"] = False
        return result
    except (TypeError, ValueError, OverflowError):
        result = _storage_error_result(
            NOTIFICATION_LOG_TABLE,
            SupabaseStorageError("invalid_payload"),
            attempts=attempts,
        )
        result["claimed"] = False
        return result


def write_notification_logs(
    entries: Iterable[Mapping[str, object]],
    *,
    supabase_url: Optional[str] = None,
    api_key: Optional[str] = None,
    supabase_api_key: Optional[str] = None,
    batch_size: int = DEFAULT_WRITE_BATCH_SIZE,
    timeout: float = DEFAULT_RAW_STORAGE_TIMEOUT,
    max_retries: int = DEFAULT_RAW_STORAGE_MAX_RETRIES,
    backoff_factor: float = DEFAULT_RAW_STORAGE_BACKOFF_SECONDS,
    max_backoff: float = DEFAULT_RAW_STORAGE_MAX_BACKOFF_SECONDS,
    sleep: Optional[Callable[[float], None]] = None,
) -> Dict[str, object]:
    """Upsert notification delivery audit rows using event_key and channel."""

    rows = [_map_notification_log(entry) for entry in entries]
    return _write_raw_rows(
        rows,
        table=NOTIFICATION_LOG_TABLE,
        conflict_columns=NOTIFICATION_LOG_CONFLICT_COLUMNS,
        supabase_url=supabase_url,
        api_key=api_key,
        supabase_api_key=supabase_api_key,
        batch_size=batch_size,
        timeout=timeout,
        max_retries=max_retries,
        backoff_factor=backoff_factor,
        max_backoff=max_backoff,
        sleep=sleep,
    )


def write_notification_log(
    entry: Mapping[str, object],
    *,
    supabase_url: Optional[str] = None,
    api_key: Optional[str] = None,
    supabase_api_key: Optional[str] = None,
    batch_size: int = DEFAULT_WRITE_BATCH_SIZE,
    timeout: float = DEFAULT_RAW_STORAGE_TIMEOUT,
    max_retries: int = DEFAULT_RAW_STORAGE_MAX_RETRIES,
    backoff_factor: float = DEFAULT_RAW_STORAGE_BACKOFF_SECONDS,
    max_backoff: float = DEFAULT_RAW_STORAGE_MAX_BACKOFF_SECONDS,
    sleep: Optional[Callable[[float], None]] = None,
) -> Dict[str, object]:
    """Upsert one notification audit row and return a safe storage result."""

    return write_notification_logs(
        [entry],
        supabase_url=supabase_url,
        api_key=api_key,
        supabase_api_key=supabase_api_key,
        batch_size=batch_size,
        timeout=timeout,
        max_retries=max_retries,
        backoff_factor=backoff_factor,
        max_backoff=max_backoff,
        sleep=sleep,
    )


def upsert_snapshot_rows(
    rows: List[Dict[str, object]],
    *,
    supabase_url: str,
    api_key: str,
    ingest_token: Optional[str] = None,
    batch_size: int = DEFAULT_WRITE_BATCH_SIZE,
) -> int:
    if not rows:
        return 0

    endpoint = (
        f"{supabase_url.rstrip('/')}/rest/v1/{HISTORY_TABLE}"
        "?on_conflict=clan_tag,race_created_at,player_tag"
    )
    headers = _supabase_headers(
        api_key,
        write=True,
        ingest_token=ingest_token,
    )

    written = 0
    for batch in _chunks(rows, batch_size):
        response = requests.post(
            endpoint,
            json=batch,
            headers=headers,
            timeout=40,
        )
        if response.status_code not in (200, 201, 204):
            raise RuntimeError(
                f"Supabase history write failed with HTTP {response.status_code}."
            )
        written += len(batch)
    return written


def snapshot_clan(
    clan_tag: str,
    clan_name: str,
    *,
    clash_api_key: str,
    supabase_url: str,
    supabase_api_key: str,
    supabase_ingest_token: Optional[str] = None,
) -> Dict[str, object]:
    members, race_items = fetch_live_clan_data(clan_tag, clash_api_key)
    rows = build_snapshot_rows(clan_tag, clan_name, members, race_items)
    written = upsert_snapshot_rows(
        rows,
        supabase_url=supabase_url,
        api_key=supabase_api_key,
        ingest_token=supabase_ingest_token,
    )
    weeks = {
        (int(row["season_id"]), str(row["race_created_at"]))
        for row in rows
    }
    return {
        "clan_tag": normalize_tag(clan_tag),
        "clan_name": clan_name,
        "rows_upserted": written,
        "weeks_seen": len(weeks),
    }
