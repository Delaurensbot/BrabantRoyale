"""Server-side T15 player screening route.

The route reads one official player profile through the T01 Clash client and
combines it with the requested player's rows from the server-side, own-clan
war history.  It does not request or scrape external war history.  The public
response contains only the allow-listed T15 screening model; the existing
admin-only leader-decision route remains the write boundary for human audit
decisions.
"""

from __future__ import annotations

from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler
import json
import os
from typing import Optional
from urllib.parse import parse_qs, urlparse

try:
    from api.clash_client import (
        ClashClientError,
        ClashRoyaleClient,
        normalize_tag as normalize_api_tag,
    )
    from api.clash_normalizers import normalize_player_profile, serialize_normalized
except ImportError:  # pragma: no cover - useful when deployed as loose files.
    from clash_client import (  # type: ignore
        ClashClientError,
        ClashRoyaleClient,
        normalize_tag as normalize_api_tag,
    )
    from clash_normalizers import normalize_player_profile, serialize_normalized

try:
    from Royale_api import CLAN_CONFIGS, DEFAULT_CLAN_TAG, get_clan_config
except ImportError:  # pragma: no cover - convenient for package-style loading.
    from ..Royale_api import CLAN_CONFIGS, DEFAULT_CLAN_TAG, get_clan_config

from scouting_metrics import build_fit_payload
from supabase_history import (
    clash_date_to_iso,
    fetch_history_rows,
    fetch_week_exclusions,
    get_supabase_read_config,
)


def _single_query_value(params: Mapping[str, list[str]], name: str) -> str:
    values = params.get(name, [])
    if len(values) > 1:
        raise ValueError("Invalid scouting request.")
    if not values:
        return ""
    value = values[0]
    if not isinstance(value, str):
        raise ValueError("Invalid scouting request.")
    return value.strip()


def _normalize_player_tag(value: object) -> str:
    try:
        return normalize_api_tag(value)  # type: ignore[arg-type]
    except ClashClientError:
        raise ValueError("Invalid player tag.") from None


def _normalize_clan_tag(value: object) -> str:
    try:
        normalized = normalize_api_tag(value)  # type: ignore[arg-type]
    except ClashClientError:
        raise ValueError("Invalid clan tag.") from None
    # Royale_api.get_clan_config historically fell back to the default clan for
    # unknown tags.  Scouting must not silently inspect a different clan.
    if normalized not in CLAN_CONFIGS:
        raise ValueError("Invalid clan tag.")
    return normalized


def parse_query(path: str) -> tuple[str, str]:
    """Parse and strictly validate the single-player scouting query."""

    try:
        parsed = urlparse(path or "")
        params = parse_qs(parsed.query, keep_blank_values=True)
    except (TypeError, ValueError):
        raise ValueError("Invalid scouting request.") from None

    player_value = _single_query_value(params, "tag")
    if not player_value:
        raise ValueError("Invalid player tag.")
    player_tag = _normalize_player_tag(player_value)
    clan_value = _single_query_value(params, "clan") or DEFAULT_CLAN_TAG
    clan_tag = _normalize_clan_tag(clan_value)
    return player_tag, clan_tag


def fetch_player(player_tag: str, api_key: str) -> dict:
    """Fetch and normalize one official player profile via the T01 client."""

    response = ClashRoyaleClient(api_key=api_key).get_player(player_tag)
    normalized = normalize_player_profile(response, player_tag=player_tag)
    result = serialize_normalized(normalized)
    return result if isinstance(result, dict) else {}


def _row_tag(row: Mapping[str, object], *keys: str) -> tuple[Optional[str], bool]:
    for key in keys:
        if key not in row:
            continue
        value = row[key]
        if value is None:
            return None, True
        try:
            return normalize_api_tag(value), True  # type: ignore[arg-type]
        except ClashClientError:
            return None, True
    return None, False


def _own_history_row_key(
    row: Mapping[str, object],
    *,
    clan_tag: str,
    player_tag: str,
) -> Optional[tuple[str, str]]:
    """Return a row key only when the row belongs to this own-clan query."""

    row_player, player_present = _row_tag(row, "player_tag", "playerTag")
    if not player_present or row_player != player_tag:
        return None
    row_clan, clan_present = _row_tag(row, "clan_tag", "clanTag")
    # A missing clan identity is ambiguous.  It must never become an own-clan
    # observation merely because the history query was otherwise scoped.
    if not clan_present or row_clan != clan_tag:
        return None
    try:
        race_key = clash_date_to_iso(
            row.get("race_created_at", row.get("raceCreatedAt"))
        )
    except (TypeError, ValueError):
        return None
    return race_key, row_player


def _filter_own_history_rows(
    rows: object,
    *,
    clan_tag: str,
    player_tag: str,
) -> list[dict[str, object]]:
    """Defensively re-check Supabase rows before they reach the war model."""

    if not isinstance(rows, (list, tuple)):
        return []
    result: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        key = _own_history_row_key(
            row,
            clan_tag=clan_tag,
            player_tag=player_tag,
        )
        if key is None or key in seen:
            continue
        seen.add(key)
        result.append(dict(row))
    return result


def _filter_own_exclusions(
    rows: object,
    *,
    clan_tag: str,
    player_tag: str,
) -> set[tuple[str, str]]:
    if not isinstance(rows, (list, tuple)):
        return set()
    result: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        key = _own_history_row_key(
            row,
            clan_tag=clan_tag,
            player_tag=player_tag,
        )
        if key is not None:
            result.add(key)
    return result


def collect_scouting_payload(player_tag: str, clan_tag: str) -> dict:
    """Collect the T15 public screening read model."""

    normalized_player_tag = _normalize_player_tag(player_tag)
    normalized_clan_tag = _normalize_clan_tag(clan_tag or DEFAULT_CLAN_TAG)
    api_key = os.environ.get("CLASH_ROYALE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Clash API configuration is unavailable.")

    clan_config = get_clan_config(normalized_clan_tag)
    player = fetch_player(normalized_player_tag, api_key)
    war_rows: list[dict[str, object]] = []
    exclusions: object = []
    supabase_config = get_supabase_read_config()
    if supabase_config:
        supabase_url, publishable_key = supabase_config
        raw_war_rows = fetch_history_rows(
            normalized_clan_tag,
            player_tag=normalized_player_tag,
            supabase_url=supabase_url,
            api_key=publishable_key,
        )
        war_rows = _filter_own_history_rows(
            raw_war_rows,
            clan_tag=normalized_clan_tag,
            player_tag=normalized_player_tag,
        )
        exclusions = fetch_week_exclusions(
            normalized_clan_tag,
            player_tag=normalized_player_tag,
            supabase_url=supabase_url,
            api_key=publishable_key,
        )

    excluded_keys = _filter_own_exclusions(
        exclusions,
        clan_tag=normalized_clan_tag,
        player_tag=normalized_player_tag,
    )
    included_war_rows = [
        row
        for row in war_rows
        if _own_history_row_key(
            row,
            clan_tag=normalized_clan_tag,
            player_tag=normalized_player_tag,
        )
        not in excluded_keys
    ]

    payload = build_fit_payload(
        player,
        included_war_rows,
        clan_tag=normalized_clan_tag,
    )
    payload["clan"] = {
        "tag": normalized_clan_tag,
        "name": clan_config.get("name"),
    }
    payload["excluded_weeks"] = len(excluded_keys)
    payload["method_version"] = "t15-v1"
    payload["observation_scope"] = "own_clan_history_only"
    return payload


def classify_error(exc: Exception) -> tuple[int, str]:
    """Map internal failures to safe public route messages."""

    if isinstance(exc, ValueError):
        return 400, "Invalid scouting request."
    if isinstance(exc, LookupError):
        return 404, "Speler niet gevonden. Controleer de player tag."
    if isinstance(exc, ClashClientError):
        if exc.code == "not_found":
            return 404, "Speler niet gevonden. Controleer de player tag."
        if exc.code in {"invalid_tag", "bad_request"}:
            return 400, "Invalid scouting request."
        return 502, "De officiële Clash API is tijdelijk niet beschikbaar."
    if isinstance(exc, RuntimeError):
        return 502, "Screeningdata is tijdelijk niet beschikbaar."
    return 500, "Screening kon niet worden geladen."


class handler(BaseHTTPRequestHandler):
    """Vercel-compatible public read adapter for the T15 screening model."""

    def _send_json(self, status_code: int, payload: Mapping[str, object]) -> None:
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
        try:
            player_tag, clan_tag = parse_query(getattr(self, "path", ""))
            payload = collect_scouting_payload(player_tag, clan_tag)
            self._send_json(200, {"ok": True, **payload})
        except Exception as exc:
            status, message = classify_error(exc)
            self._send_json(status, {"ok": False, "error": message})

    def do_POST(self) -> None:
        self._send_json(405, {"ok": False, "error": "Method not allowed."})

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST


__all__ = [
    "collect_scouting_payload",
    "classify_error",
    "fetch_player",
    "handler",
    "parse_query",
]
