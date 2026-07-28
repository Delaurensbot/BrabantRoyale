from http.server import BaseHTTPRequestHandler
import json
import os
from urllib.parse import parse_qs, urlparse

import requests

from Royale_api import DEFAULT_CLAN_TAG, get_clan_config
from scouting_metrics import build_fit_payload
from supabase_history import (
    clash_date_to_iso,
    fetch_history_rows,
    fetch_week_exclusions,
    get_supabase_read_config,
    normalize_tag,
)


ROYAL_API_BASE_URL = "https://proxy.royaleapi.dev/v1"


def parse_query(path: str) -> tuple[str, str]:
    params = parse_qs(urlparse(path).query)
    player_tag = params.get("tag", [""])[0]
    clan_tag = params.get("clan", [DEFAULT_CLAN_TAG])[0]
    return normalize_tag(player_tag), normalize_tag(clan_tag)


def fetch_player(player_tag: str, api_key: str) -> dict:
    response = requests.get(
        f"{ROYAL_API_BASE_URL}/players/%23{player_tag}",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=25,
    )
    if response.status_code == 404:
        raise LookupError("Speler niet gevonden. Controleer de player tag.")
    if response.status_code != 200:
        raise RuntimeError(
            f"Clash API error {response.status_code} for player profile."
        )
    return response.json() if response.content else {}


def collect_scouting_payload(player_tag: str, clan_tag: str) -> dict:
    api_key = os.environ.get("CLASH_ROYALE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("Missing CLASH_ROYALE_API_KEY environment variable.")
    if not player_tag:
        raise ValueError("Vul een geldige player tag in.")

    clan_config = get_clan_config(clan_tag or DEFAULT_CLAN_TAG)
    normalized_clan_tag = normalize_tag(clan_config.get("tag"))
    player = fetch_player(player_tag, api_key)

    war_rows = []
    exclusions = []
    supabase_config = get_supabase_read_config()
    if supabase_config:
        supabase_url, publishable_key = supabase_config
        war_rows = fetch_history_rows(
            normalized_clan_tag,
            player_tag=player_tag,
            supabase_url=supabase_url,
            api_key=publishable_key,
        )
        exclusions = fetch_week_exclusions(
            normalized_clan_tag,
            player_tag=player_tag,
            supabase_url=supabase_url,
            api_key=publishable_key,
        )

    excluded_snapshots = set()
    for row in exclusions:
        try:
            excluded_snapshots.add(
                (
                    clash_date_to_iso(row.get("race_created_at")),
                    normalize_tag(row.get("player_tag")),
                )
            )
        except (TypeError, ValueError):
            continue

    included_war_rows = []
    for row in war_rows:
        try:
            key = (
                clash_date_to_iso(row.get("race_created_at")),
                normalize_tag(row.get("player_tag")),
            )
        except (TypeError, ValueError):
            continue
        if key not in excluded_snapshots:
            included_war_rows.append(row)

    payload = build_fit_payload(
        player,
        included_war_rows,
        clan_tag=normalized_clan_tag,
    )
    payload["clan"] = {
        "tag": normalized_clan_tag,
        "name": clan_config.get("name"),
    }
    payload["excluded_weeks"] = len(excluded_snapshots)
    payload["method_version"] = "fit-v1"
    return payload


def classify_error(exc: Exception) -> tuple[int, str]:
    if isinstance(exc, ValueError):
        return 400, str(exc)
    if isinstance(exc, LookupError):
        return 404, str(exc)
    message = str(exc)
    if "Clash API error" in message:
        return 502, "De officiële Clash API reageerde niet goed. Probeer opnieuw."
    return 500, message


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            player_tag, clan_tag = parse_query(self.path)
            payload = collect_scouting_payload(player_tag, clan_tag)
            body = json.dumps(
                {"ok": True, **payload},
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
        except Exception as exc:
            status, message = classify_error(exc)
            body = json.dumps(
                {"ok": False, "error": message},
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(status)

        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

