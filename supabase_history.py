"""Supabase-backed history for Clash Royale clan-war analytics."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

import requests


ROYAL_API_BASE_URL = "https://proxy.royaleapi.dev/v1"
HISTORY_TABLE = "clan_war_player_weeks"
EXCLUSIONS_TABLE = "clan_war_week_exclusions"
DEFAULT_PAGE_SIZE = 1000
DEFAULT_WRITE_BATCH_SIZE = 500
DEFAULT_SUPABASE_URL = "https://upbjlamddxooxhxhkivg.supabase.co"
DEFAULT_SUPABASE_PUBLISHABLE_KEY = (
    "sb_publishable_gWj42LLCw4odVjLdRecWrw_4xeQlF9i"
)


def normalize_tag(value: object) -> str:
    return str(value or "").strip().replace("%23", "").replace("#", "").upper()


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
    response = requests.get(
        f"{ROYAL_API_BASE_URL}{path}",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=timeout,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Clash API error {response.status_code} for {path}")
    return response.json() if response.content else {}


def fetch_live_clan_data(
    clan_tag: str,
    api_key: str,
) -> Tuple[List[Dict[str, object]], List[Dict[str, object]]]:
    norm_tag = normalize_tag(clan_tag)
    encoded_tag = f"%23{norm_tag}"
    members_payload = fetch_clash_json(f"/clans/{encoded_tag}/members", api_key)
    members = list(members_payload.get("items") or [])

    race_items: List[Dict[str, object]] = []
    river_log = fetch_clash_json(f"/clans/{encoded_tag}/riverracelog", api_key)
    race_items.extend(river_log.get("items") or [])

    try:
        current_race = fetch_clash_json(
            f"/clans/{encoded_tag}/currentriverrace",
            api_key,
        )
        if current_race:
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
    except Exception as exc:
        return [], {
            "enabled": True,
            "source": "clash_api_fallback",
            "message": str(exc),
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
    except Exception as exc:
        return [], {
            "enabled": True,
            "message": str(exc),
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
