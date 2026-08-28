"""Server-side roster snapshots and observed member lifecycle data.

The official Clash Royale API exposes the current member list, not exact join
or leave events.  This route therefore stores complete roster observations in
Supabase and describes lifecycle changes only as intervals between snapshots.
No local file is used as a fallback: a storage failure is surfaced as an
explicit ``error``/``unknown`` status instead of being converted into a join,
leave, or zero-valued performance result.
"""

from collections import defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
import json
import math
import os
import re
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, unquote, urlparse

try:
    from api.config import CLAN_CONFIGS, DEFAULT_CLAN_TAG, get_clan_config
except ImportError:  # pragma: no cover - useful when loaded as a loose file.
    from config import CLAN_CONFIGS, DEFAULT_CLAN_TAG, get_clan_config

try:
    from api.clash_client import (
        ROYAL_API_BASE_URL,
        ClashClientError,
        ClashRoyaleClient,
    )
except ImportError:  # pragma: no cover - convenient for loose-file deployment.
    from clash_client import ROYAL_API_BASE_URL, ClashClientError, ClashRoyaleClient

try:
    from supabase_history import read_roster_snapshots, write_roster_snapshots
except ImportError:  # pragma: no cover - convenient for package-style loading.
    from ..supabase_history import (  # type: ignore
        read_roster_snapshots,
        write_roster_snapshots,
    )


OBSERVED_INTERVAL_LABEL = "waargenomen tussen snapshots"
ROSTER_STALE_AFTER_SECONDS = 2 * 60 * 60
_STORAGE_STATUSES = frozenset(
    {"ok", "empty", "fresh", "stale", "partial", "error", "unknown"}
)
_SAFE_STORAGE_ERRORS = frozenset(
    {
        "configuration_error",
        "server_key_required",
        "timeout",
        "transport_error",
        "rate_limited",
        "upstream_server_error",
        "supabase_http_error",
        "invalid_response",
        "invalid_payload",
        "storage_unavailable",
    }
)
_SECRET_ENV_NAMES = (
    "CLASH_ROYALE_API_KEY",
    "SUPABASE_SECRET_KEY",
    "SUPABASE_SERVICE_ROLE_KEY",
    "SUPABASE_PUBLISHABLE_KEY",
    "SUPABASE_ANON_KEY",
    "SUPABASE_INGEST_TOKEN",
    "WAR_MONITOR_SECRET",
    "WAR_STATUS_LEADER_SECRET",
)


def parse_limit_from_query(path: str) -> int:
    parsed = urlparse(path or "")
    params = parse_qs(parsed.query)
    if "limit" in params and params["limit"]:
        try:
            return int(params["limit"][0])
        except (TypeError, ValueError):
            return 10
    return 10


def parse_clan_from_query(path: str) -> str:
    parsed = urlparse(path or "")
    params = parse_qs(parsed.query)
    if "clan" in params and params["clan"]:
        return params["clan"][0]
    return ""


def normalize_player_tag(raw_tag: object) -> str:
    """Return the canonical stable identity used for roster comparisons."""

    if not isinstance(raw_tag, str):
        return ""
    clean = raw_tag.strip()
    for _ in range(2):
        decoded = unquote(clean)
        if decoded == clean:
            break
        clean = decoded
    if clean.startswith("#"):
        clean = clean[1:]
    if not re.fullmatch(r"[A-Za-z0-9]{1,32}", clean):
        return ""
    return clean.upper()


def _timestamp_to_iso(value: object) -> Optional[str]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        parsed = None
        for pattern in ("%Y%m%dT%H%M%S.%fZ", "%Y%m%dT%H%M%SZ"):
            try:
                parsed = datetime.strptime(raw, pattern).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        if parsed is None:
            if raw.endswith(" UTC"):
                raw = raw[:-4].strip() + "+00:00"
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except (TypeError, ValueError, OverflowError):
                return None
    else:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    normalized = parsed.astimezone(timezone.utc)
    timespec = "microseconds" if normalized.microsecond else "seconds"
    return normalized.isoformat(timespec=timespec).replace("+00:00", "Z")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00",
        "Z",
    )


def _legacy_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _safe_member_text(value: object, default: Optional[str] = None) -> Optional[str]:
    if value is None:
        return default
    result = str(value).strip()
    if not result or any(ord(character) < 32 or ord(character) == 127 for character in result):
        return default
    for env_name in _SECRET_ENV_NAMES:
        secret = os.environ.get(env_name, "").strip()
        if secret and secret in result:
            return default
    return result


def _safe_trophies(value: object) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 0 else None


def _member_info(member: object) -> Optional[Dict[str, object]]:
    if not isinstance(member, Mapping):
        return None
    tag = normalize_player_tag(
        member.get("player_tag", member.get("playerTag", member.get("tag")))
    )
    if not tag:
        return None
    name = _safe_member_text(
        member.get("player_name", member.get("playerName", member.get("name")))
    )
    role = _safe_member_text(
        member.get("role", member.get("player_role", member.get("playerRole"))),
        "unknown",
    ) or "unknown"
    return {
        "tag": tag,
        "name": name,
        "role": role,
        "trophies": _safe_trophies(member.get("trophies")),
    }


def _extract_member_items(
    payload: object,
) -> Tuple[List[Dict[str, object]], str, List[str]]:
    """Extract a complete/partial/empty roster without inventing members."""

    if not isinstance(payload, Mapping):
        return [], "unknown", ["items"]
    if "items" not in payload:
        return [], "unknown", ["items"]
    raw_items = payload.get("items")
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes, bytearray)):
        return [], "partial", ["items"]
    if not raw_items:
        return [], "empty", []

    members: Dict[str, Dict[str, object]] = {}
    invalid_count = 0
    for raw_member in raw_items:
        info = _member_info(raw_member)
        if info is None:
            invalid_count += 1
            continue
        # The player tag is the identity.  A later duplicate in one response
        # updates its display data rather than creating a second player.
        members[str(info["tag"])] = info

    if not members:
        return [], "partial", ["player_tag"]
    return list(members.values()), "partial" if invalid_count else "fresh", (
        ["player_tag"] if invalid_count else []
    )


def build_roster_snapshot_rows(
    members: Iterable[Mapping[str, object]],
    clan_tag: str,
    *,
    captured_at: Optional[object] = None,
    seen_at: Optional[object] = None,
) -> List[Dict[str, object]]:
    """Build one normalized row per stable player identity."""

    normalized_clan = normalize_player_tag(clan_tag)
    if not normalized_clan:
        raise ValueError("clan_tag is required.")
    captured = _timestamp_to_iso(captured_at) if captured_at is not None else _now_iso()
    if captured is None:
        raise ValueError("captured_at must be a valid timestamp.")
    seen = _timestamp_to_iso(seen_at) if seen_at is not None else captured
    if seen is None:
        raise ValueError("seen_at must be a valid timestamp.")

    deduped: Dict[str, Dict[str, object]] = {}
    for member in members:
        info = _member_info(member)
        if info is None:
            continue
        tag = str(info["tag"])
        deduped[tag] = {
            "clan_tag": normalized_clan,
            "player_tag": tag,
            "player_name": info.get("name"),
            "role": info.get("role") or "unknown",
            "trophies": info.get("trophies"),
            "seen_at": seen,
            "captured_at": captured,
        }
    return list(deduped.values())


def api_get(path: str, api_key: str) -> dict:
    """Fetch members through the central T01 client.

    The narrow path check preserves the old injectable ``api_get`` seam for
    tests while preventing this route from growing a second authentication or
    retry implementation.
    """

    match = re.fullmatch(r"/clans/%23([A-Za-z0-9]{1,32})/members", path or "")
    if not match:
        raise ValueError("Unsupported roster API endpoint.")
    response = ClashRoyaleClient(api_key=api_key).get_members(match.group(1))
    payload = response.data
    if not isinstance(payload, Mapping):
        raise RuntimeError("Official Clash API returned invalid JSON.")
    return dict(payload)


def _observed_interval(start: object, end: object) -> Optional[Dict[str, object]]:
    start_iso = _timestamp_to_iso(start)
    end_iso = _timestamp_to_iso(end)
    if start_iso is None or end_iso is None:
        return None
    return {
        "from": start_iso,
        "to": end_iso,
        "label": OBSERVED_INTERVAL_LABEL,
    }


def _normalize_history_row(
    raw: object,
    requested_clan: Optional[str],
) -> Optional[Dict[str, object]]:
    if not isinstance(raw, Mapping):
        return None
    row_clan = normalize_player_tag(raw.get("clan_tag", raw.get("clanTag")))
    player_tag = normalize_player_tag(
        raw.get("player_tag", raw.get("playerTag", raw.get("tag")))
    )
    if not row_clan or not player_tag:
        return None
    if requested_clan and row_clan != requested_clan:
        return None
    captured = _timestamp_to_iso(raw.get("captured_at", raw.get("capturedAt")))
    if captured is None:
        return None
    seen = _timestamp_to_iso(raw.get("seen_at", raw.get("seenAt"))) or captured
    name = _safe_member_text(
        raw.get("player_name", raw.get("playerName", raw.get("name")))
    )
    role = _safe_member_text(
        raw.get("role", raw.get("player_role", raw.get("playerRole"))),
        "unknown",
    ) or "unknown"
    return {
        "clan_tag": row_clan,
        "player_tag": player_tag,
        "player_name": name,
        "role": role,
        "trophies": _safe_trophies(raw.get("trophies")),
        "seen_at": seen,
        "captured_at": captured,
    }


def _empty_history(
    status: str = "unknown",
    *,
    requested_clan: Optional[str] = None,
) -> Dict[str, object]:
    return {
        "status": status,
        "data_status": status,
        "clan_tag": requested_clan,
        "snapshot_count": 0,
        "invalid_rows": 0,
        "snapshot_times": [],
        "players": [],
        "members": [],
        "joins": [],
        "leaves": [],
        "observed_leaves": [],
        "confirmed_leaves": [],
        "missing_from_last_snapshot": [],
        "role_changes": [],
    }


def _lifecycle_event_row(
    lifecycle: Mapping[str, object],
    row: Mapping[str, object],
    *,
    event_type: str,
    interval: Mapping[str, object],
    confirmed_leave: bool = False,
) -> Dict[str, object]:
    tag = str(lifecycle["player_tag"])
    display_name = str(row.get("player_name") or tag)
    leave_status = (
        "confirmed_observed_leave"
        if confirmed_leave
        else "observed_absence"
        if event_type == "observed_leave"
        else None
    )
    result: Dict[str, object] = {
        "name": display_name,
        "pid": tag,
        "player_tag": tag,
        "player_name": display_name,
        "role": row.get("role") or "unknown",
        "trophies": row.get("trophies"),
        "url": f"https://royaleapi.com/player/{tag}",
        # Keep the old join.html fields, but do not imply an exact event time.
        "ago": OBSERVED_INTERVAL_LABEL,
        "utc": OBSERVED_INTERVAL_LABEL,
        "first_seen_in_clan": lifecycle.get("first_seen_in_clan"),
        "last_seen_in_clan": lifecycle.get("last_seen_in_clan"),
        "observed_join_interval": (
            interval
            if event_type == "observed_join"
            else lifecycle.get("observed_join_interval")
        ),
        "observed_leave_interval": (
            interval
            if event_type == "observed_leave"
            else lifecycle.get("observed_leave_interval")
        ),
        "role_changes": list(lifecycle.get("role_changes") or []),
        "event_type": event_type,
        "confirmed_leave": bool(confirmed_leave),
        "leave_status": leave_status,
        "present_in_last_snapshot": bool(lifecycle.get("present_in_last_snapshot")),
        "data_status": "observed",
    }
    return result


def calculate_roster_history(
    snapshot_rows: Iterable[Mapping[str, object]],
    clan_tag: Optional[str] = None,
) -> Dict[str, object]:
    """Derive observed lifecycle intervals from durable roster rows.

    A first-ever appearance is reported as ``first_seen_in_clan`` but not as
    a join, because there is no earlier observation to compare.  One missing
    latest snapshot is retained as an unconfirmed absence.  A leave is marked
    confirmed only after the player is absent from two consecutive complete
    snapshots; the interval still describes observations, never an exact game
    event.
    """

    requested_clan = normalize_player_tag(clan_tag) if clan_tag else None
    if clan_tag and not requested_clan:
        return _empty_history("unknown")
    normalized_rows: Dict[Tuple[str, str, str], Dict[str, object]] = {}
    clans = set()
    invalid_rows = 0
    for raw in snapshot_rows or []:
        row = _normalize_history_row(raw, requested_clan)
        if row is None:
            invalid_rows += 1
            continue
        row_clan = str(row["clan_tag"])
        clans.add(row_clan)
        key = (
            row_clan,
            str(row["player_tag"]),
            str(row["captured_at"]),
        )
        # A duplicate natural key is one observation; the latest normalized
        # display fields win without creating a second player.
        normalized_rows[key] = row

    if requested_clan is None:
        if len(clans) > 1:
            return _empty_history("unknown")
        requested_clan = next(iter(clans), None)

    if not normalized_rows:
        empty = _empty_history(
            "partial" if invalid_rows else "unknown",
            requested_clan=requested_clan,
        )
        empty["invalid_rows"] = invalid_rows
        return empty

    rows = sorted(
        normalized_rows.values(),
        key=lambda row: (str(row["captured_at"]), str(row["player_tag"])),
    )
    snapshot_rows_by_time: Dict[str, List[Dict[str, object]]] = defaultdict(list)
    for row in rows:
        snapshot_rows_by_time[str(row["captured_at"])].append(row)
    snapshot_times = sorted(snapshot_rows_by_time)
    presence = [
        {str(row["player_tag"]) for row in snapshot_rows_by_time[captured_at]}
        for captured_at in snapshot_times
    ]
    by_player: Dict[str, List[Tuple[int, Dict[str, object]]]] = defaultdict(list)
    for index, captured_at in enumerate(snapshot_times):
        for row in snapshot_rows_by_time[captured_at]:
            by_player[str(row["player_tag"])].append((index, row))

    players: List[Dict[str, object]] = []
    join_events: List[Dict[str, object]] = []
    leave_events: List[Dict[str, object]] = []
    missing_latest: List[Dict[str, object]] = []
    flattened_role_changes: List[Dict[str, object]] = []

    for tag in sorted(by_player):
        observations = by_player[tag]
        first_index, first_row = observations[0]
        last_index, last_row = observations[-1]
        role_changes: List[Dict[str, object]] = []
        for (previous_index, previous_row), (current_index, current_row) in zip(
            observations,
            observations[1:],
        ):
            # Only adjacent roster observations establish a role transition;
            # a gap may be a leave/rejoin rather than a role change.
            if current_index != previous_index + 1:
                continue
            previous_role = str(previous_row.get("role") or "unknown")
            current_role = str(current_row.get("role") or "unknown")
            if (
                previous_role.casefold() == "unknown"
                or current_role.casefold() == "unknown"
                or previous_role.casefold() == current_role.casefold()
            ):
                continue
            interval = _observed_interval(
                snapshot_times[previous_index],
                snapshot_times[current_index],
            )
            if interval is None:
                continue
            role_change = {
                "from": previous_role,
                "to": current_role,
                "observed_between_snapshots": interval,
            }
            role_changes.append(role_change)

        lifecycle: Dict[str, object] = {
            "player_tag": tag,
            "player_name": str(last_row.get("player_name") or tag),
            "name": str(last_row.get("player_name") or tag),
            "role": last_row.get("role") or "unknown",
            "trophies": last_row.get("trophies"),
            "first_seen_in_clan": first_row.get("seen_at") or snapshot_times[first_index],
            "last_seen_in_clan": last_row.get("seen_at") or snapshot_times[last_index],
            "observed_join_interval": None,
            "observed_leave_interval": None,
            "role_changes": role_changes,
            "present_in_last_snapshot": last_index == len(snapshot_times) - 1,
            "confirmed_leave": False,
            "leave_status": "present" if last_index == len(snapshot_times) - 1 else "unknown",
            "data_status": "observed",
            "url": f"https://royaleapi.com/player/{tag}",
        }

        for current_index, current_row in observations:
            if current_index <= 0 or tag in presence[current_index - 1]:
                continue
            interval = _observed_interval(
                snapshot_times[current_index - 1],
                snapshot_times[current_index],
            )
            if interval is None:
                continue
            lifecycle["observed_join_interval"] = interval
            event = _lifecycle_event_row(
                lifecycle,
                current_row,
                event_type="observed_join",
                interval=interval,
            )
            join_events.append(event)

        for current_index in range(1, len(snapshot_times)):
            if tag not in presence[current_index - 1] or tag in presence[current_index]:
                continue
            previous_row = next(
                row
                for index, row in observations
                if index == current_index - 1
            )
            interval = _observed_interval(
                snapshot_times[current_index - 1],
                snapshot_times[current_index],
            )
            if interval is None:
                continue
            confirmed = (
                current_index + 1 < len(snapshot_times)
                and tag not in presence[current_index + 1]
            )
            lifecycle["observed_leave_interval"] = interval
            lifecycle["confirmed_leave"] = bool(confirmed)
            lifecycle["leave_status"] = (
                "confirmed_observed_leave"
                if confirmed
                else "observed_absence"
            )
            event = _lifecycle_event_row(
                lifecycle,
                previous_row,
                event_type="observed_leave",
                interval=interval,
                confirmed_leave=confirmed,
            )
            leave_events.append(event)

        if not lifecycle["present_in_last_snapshot"]:
            first_absent_index = last_index + 1
            interval = _observed_interval(
                snapshot_times[last_index],
                snapshot_times[first_absent_index],
            )
            absence_count = len(snapshot_times) - last_index - 1
            confirmed = absence_count >= 2
            lifecycle["observed_leave_interval"] = interval
            lifecycle["confirmed_leave"] = confirmed
            lifecycle["leave_status"] = (
                "confirmed_observed_leave"
                if confirmed
                else "not_present_in_last_snapshot"
            )
            missing = dict(lifecycle)
            missing["missing_from_last_snapshot"] = True
            missing_latest.append(missing)

        players.append(lifecycle)
        for role_change in role_changes:
            flattened_role_changes.append(
                {
                    "player_tag": tag,
                    "player_name": lifecycle["player_name"],
                    **role_change,
                }
            )

    join_events.sort(
        key=lambda event: (
            str((event.get("observed_join_interval") or {}).get("to", "")),
            str(event.get("player_tag", "")),
        ),
        reverse=True,
    )
    leave_events.sort(
        key=lambda event: (
            str((event.get("observed_leave_interval") or {}).get("to", "")),
            str(event.get("player_tag", "")),
        ),
        reverse=True,
    )
    missing_latest.sort(key=lambda row: str(row.get("player_tag", "")))
    players.sort(key=lambda row: str(row.get("player_tag", "")))
    current_members = [
        player for player in players if player.get("present_in_last_snapshot") is True
    ]

    history_status = "partial" if invalid_rows else "observed"
    return {
        "status": history_status,
        "data_status": history_status,
        "clan_tag": requested_clan,
        "snapshot_count": len(snapshot_times),
        "invalid_rows": invalid_rows,
        "snapshot_times": snapshot_times,
        "players": players,
        "members": current_members,
        "joins": join_events,
        "leaves": leave_events,
        "observed_leaves": leave_events,
        "confirmed_leaves": [
            event for event in leave_events if event.get("confirmed_leave") is True
        ],
        "missing_from_last_snapshot": missing_latest,
        "role_changes": flattened_role_changes,
    }


def _suppress_unreliable_events(history: Dict[str, object]) -> Dict[str, object]:
    """Keep observations but remove transitions when storage is incomplete."""

    result = dict(history)
    result["joins"] = []
    result["leaves"] = []
    result["observed_leaves"] = []
    result["confirmed_leaves"] = []
    result["missing_from_last_snapshot"] = []
    result["role_changes"] = []
    players = []
    for player in history.get("players") or []:
        if not isinstance(player, Mapping):
            continue
        copy = dict(player)
        copy["observed_join_interval"] = None
        copy["observed_leave_interval"] = None
        copy["role_changes"] = []
        copy["confirmed_leave"] = False
        copy["data_status"] = "unknown"
        copy["leave_status"] = (
            "present"
            if copy.get("present_in_last_snapshot")
            else "unknown"
        )
        players.append(copy)
    result["players"] = players
    result["members"] = [
        player for player in players if player.get("present_in_last_snapshot") is True
    ]
    return result


def _storage_status(result: object, default: str = "error") -> str:
    if not isinstance(result, Mapping):
        return default
    value = result.get("status") or result.get("data_status")
    candidate = str(value).strip().lower() if value is not None else ""
    if candidate in {"stored", "success"}:
        return "ok"
    if not candidate and isinstance(result.get("ok"), bool):
        return "ok" if result.get("ok") is True else default
    return candidate if candidate in _STORAGE_STATUSES else default


def _safe_storage_projection(result: object) -> Dict[str, object]:
    """Expose only non-sensitive storage metadata in the public response."""

    status = _storage_status(result)
    projection: Dict[str, object] = {"status": status}
    if isinstance(result, Mapping):
        error = result.get("error")
        if isinstance(error, str) and error in _SAFE_STORAGE_ERRORS:
            projection["error"] = error
        for key in (
            "rows_written",
            "rows_upserted",
            "count",
            "batches",
            "attempts",
            "status_code",
        ):
            value = result.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                projection[key] = value
        if isinstance(result.get("truncated"), bool):
            projection["truncated"] = result["truncated"]
    return projection


def _safe_storage_bundle(value: object) -> Dict[str, object]:
    if not isinstance(value, Mapping):
        return {
            "read": _safe_storage_projection(None),
            "write": _safe_storage_projection(None),
        }
    return {
        "read": _safe_storage_projection(value.get("read")),
        "write": _safe_storage_projection(value.get("write")),
    }


def _storage_write_succeeded(result: object) -> bool:
    if not isinstance(result, Mapping):
        return False
    if result.get("ok") is False:
        return False
    return _storage_status(result) in {"ok", "fresh"}


def _safe_storage_failure() -> Dict[str, object]:
    return {
        "ok": False,
        "status": "error",
        "data_status": "error",
        "error": "storage_unavailable",
        "rows_written": 0,
        "attempts": 0,
    }


def _note_for_status(status: str) -> str:
    if status == "fresh":
        return (
            "Join/leave history uses server-side Supabase roster snapshots; "
            "tijden zijn waargenomen tussen snapshots."
        )
    if status == "empty":
        return "Rosterdata is leeg; joins en leaves zijn niet afgeleid."
    if status == "partial":
        return (
            "Rosterdata is gedeeltelijk; joins, leaves en rolwijzigingen zijn "
            "onbekend totdat een volledige snapshot beschikbaar is."
        )
    if status == "stale":
        return "Rosterdata is stale; lifecycle-informatie is niet actueel bevestigd."
    if status == "error":
        return (
            "Duurzame rosteropslag is tijdelijk niet beschikbaar; "
            "join/leave-informatie is onbekend."
        )
    return "Rosterdata is onbekend; joins en leaves zijn niet afgeleid."


def _safe_current_member(info: Mapping[str, object]) -> Dict[str, object]:
    tag = str(info.get("tag") or "")
    return {
        "player_tag": tag,
        "pid": tag,
        "name": str(info.get("name") or tag),
        "player_name": str(info.get("name") or tag),
        "role": info.get("role") or "unknown",
        "trophies": info.get("trophies"),
        "url": f"https://royaleapi.com/player/{tag}",
    }


def _validate_route_clan(raw_clan: str) -> str:
    """Accept only repository-configured clans at the public route boundary."""

    candidate = normalize_player_tag(raw_clan or DEFAULT_CLAN_TAG)
    configured = {
        normalize_player_tag(config_key)
        for config_key in CLAN_CONFIGS
    }
    if not candidate or candidate not in configured:
        raise ValueError("Invalid clan tag.")
    return candidate


def collect_join_data_official(limit: int, clan_tag: str) -> dict:
    """Fetch the current roster and report only observed lifecycle changes."""

    limit = max(1, min(50, int(limit)))
    api_key = os.environ.get("CLASH_ROYALE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing CLASH_ROYALE_API_KEY environment variable.")

    clan_config = get_clan_config(clan_tag)
    norm_clan_tag = normalize_player_tag(clan_config["tag"])
    encoded = f"%23{norm_clan_tag}"
    members_payload = api_get(f"/clans/{encoded}/members", api_key)
    members, current_status, missing_fields = _extract_member_items(members_payload)
    fetched_at = _legacy_now()
    captured_at = _now_iso()
    current_rows = (
        build_roster_snapshot_rows(
            members,
            norm_clan_tag,
            captured_at=captured_at,
            seen_at=captured_at,
        )
        if current_status == "fresh"
        else []
    )

    read_result: Dict[str, object] = {
        "status": "unknown",
        "data_status": "unknown",
        "snapshots": None,
    }
    write_result: Dict[str, object] = {
        "status": "unknown",
        "data_status": "unknown",
    }
    stored_rows: List[Mapping[str, object]] = []
    history = _empty_history(current_status, requested_clan=norm_clan_tag)

    if current_status == "fresh" and current_rows:
        try:
            read_value = read_roster_snapshots(
                norm_clan_tag,
                stale_after_seconds=ROSTER_STALE_AFTER_SECONDS,
            )
            if isinstance(read_value, list):
                read_result = {
                    "status": "fresh" if read_value else "empty",
                    "data_status": "fresh" if read_value else "empty",
                    "snapshots": read_value,
                }
                raw_stored = read_value
            elif isinstance(read_value, Mapping):
                read_result = dict(read_value)
                raw_stored = read_value.get(
                    "snapshots",
                    read_value.get("rows", read_value.get("data")),
                )
            else:
                raw_stored = None
            if isinstance(raw_stored, list):
                stored_rows = [
                    row for row in raw_stored if isinstance(row, Mapping)
                ]
        except Exception:
            read_result = _safe_storage_failure()

        try:
            write_value = write_roster_snapshots(current_rows)
            if isinstance(write_value, Mapping):
                write_result = dict(write_value)
            else:
                write_result = _safe_storage_failure()
        except Exception:
            write_result = _safe_storage_failure()

        history = calculate_roster_history(
            [*stored_rows, *current_rows],
            norm_clan_tag,
        )
        read_status = _storage_status(read_result)
        write_status = _storage_status(write_result)
        if (
            read_status in {"error", "partial", "unknown"}
            or not _storage_write_succeeded(write_result)
        ):
            history = _suppress_unreliable_events(history)
            overall_status = (
                "error"
                if "error" in {read_status, write_status}
                else "partial"
            )
        else:
            overall_status = "fresh"
        history["status"] = overall_status
        history["data_status"] = (
            "observed" if overall_status == "fresh" else overall_status
        )
    elif current_status in {"empty", "partial", "unknown"}:
        # Do not query or write an incomplete/empty response: it cannot safely
        # prove that anyone left the clan.
        history = _empty_history(current_status, requested_clan=norm_clan_tag)

    if current_status == "fresh" and not current_rows:
        current_status = "partial"
        history = _empty_history(current_status, requested_clan=norm_clan_tag)

    joins = list(history.get("joins") or [])[:limit]
    roster_status = str(history.get("status") or current_status)
    if current_status != "fresh":
        roster_status = current_status

    lifecycle_by_tag = {
        str(row.get("player_tag")): row
        for row in history.get("players") or []
        if isinstance(row, Mapping)
    }
    current_members = []
    for info in members:
        projected = _safe_current_member(info)
        lifecycle = lifecycle_by_tag.get(str(projected["player_tag"]))
        if lifecycle:
            for field in (
                "first_seen_in_clan",
                "last_seen_in_clan",
                "observed_join_interval",
                "observed_leave_interval",
                "role_changes",
                "present_in_last_snapshot",
                "confirmed_leave",
                "leave_status",
                "data_status",
            ):
                projected[field] = lifecycle.get(field)
        current_members.append(projected)
    return {
        "fetched_at": fetched_at,
        "captured_at": captured_at,
        "source_url": f"{ROYAL_API_BASE_URL}/clans/%23{norm_clan_tag}/members",
        "clan_tag": norm_clan_tag,
        "clan_name": clan_config.get("name"),
        "joins": joins,
        "leaves": list(history.get("leaves") or []),
        "confirmed_leaves": list(history.get("confirmed_leaves") or []),
        "missing_from_last_snapshot": list(
            history.get("missing_from_last_snapshot") or []
        ),
        "role_changes": list(history.get("role_changes") or []),
        "members": current_members,
        "roster": history,
        "lifecycle": list(history.get("players") or []),
        "player_history": list(history.get("players") or []),
        "roster_status": roster_status,
        "roster_data_status": history.get("data_status") or roster_status,
        "missing_fields": missing_fields,
        "storage": {
            "read": _safe_storage_projection(read_result),
            "write": _safe_storage_projection(write_result),
        },
        "note": _note_for_status(roster_status),
    }


def collect_join_data(
    limit: int = 10,
    clan_tag: str = DEFAULT_CLAN_TAG,
) -> dict:
    """Compatibility wrapper for callers that used the generic collector name."""

    return collect_join_data_official(limit=limit, clan_tag=clan_tag)


def classify_error(exc: Exception) -> Tuple[int, str]:
    """Map failures to messages that cannot contain keys or upstream payloads."""

    if isinstance(exc, ClashClientError):
        if getattr(exc, "code", "") == "configuration_error":
            return 500, "Server configuration is incomplete."
        return 502, "Official Clash API request failed. Try again shortly."
    message = str(exc).lower()
    if "missing clash_royale_api_key" in message:
        return 500, "Server configuration is incomplete."
    if "clash api error" in message:
        return 502, "Official Clash API request failed. Try again shortly."
    if any(token in message for token in ("httpsconnectionpool", "network", "proxy")):
        return 502, "Network/proxy error while contacting official Clash API. Retry shortly."
    if "supabase" in message or "storage" in message:
        return 502, "Roster storage is temporarily unavailable."
    return 500, "Join data is temporarily unavailable."


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            limit = max(1, min(50, parse_limit_from_query(self.path)))
            clan_tag = _validate_route_clan(parse_clan_from_query(self.path))
            data = collect_join_data_official(
                limit=limit,
                clan_tag=clan_tag,
            )

            payload = {
                "ok": True,
                "fetched_at": data["fetched_at"],
                "source_url": data["source_url"],
                "clan_tag": data.get("clan_tag"),
                "clan_name": data.get("clan_name"),
                "limit": limit,
                # Existing join.html reads this unchanged.
                "joins": data["joins"],
                "note": data.get("note"),
                "captured_at": data.get("captured_at"),
                "roster_status": data.get("roster_status"),
                "roster_data_status": data.get("roster_data_status"),
                "storage": _safe_storage_bundle(data.get("storage")),
                "roster": data.get("roster"),
                "lifecycle": data.get("lifecycle"),
                "player_history": data.get("player_history"),
                "leaves": data.get("leaves"),
                "confirmed_leaves": data.get("confirmed_leaves"),
                "missing_from_last_snapshot": data.get("missing_from_last_snapshot"),
                "role_changes": data.get("role_changes"),
                "members": data.get("members"),
                "missing_fields": data.get("missing_fields"),
            }

            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        except ValueError:
            status_code = 400
            friendly_message = "Invalid clan tag."
            payload = {
                "ok": False,
                "error": friendly_message,
            }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            status_code, friendly_message = classify_error(exc)
            # Do not serialize ``str(exc)``: request exceptions may include
            # deployment URLs, headers, or API/admin keys.
            payload = {
                "ok": False,
                "error": friendly_message,
            }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)


__all__ = [
    "OBSERVED_INTERVAL_LABEL",
    "build_roster_snapshot_rows",
    "calculate_roster_history",
    "classify_error",
    "collect_join_data",
    "collect_join_data_official",
    "handler",
    "normalize_player_tag",
    "parse_clan_from_query",
    "parse_limit_from_query",
]
