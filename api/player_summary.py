from http.server import BaseHTTPRequestHandler
import json
import os
from typing import Dict, List, Tuple
from urllib.parse import parse_qs, urlparse

import requests

from Royale_api import DEFAULT_CLAN_TAG, get_clan_config

ROYAL_API_BASE_URL = "https://proxy.royaleapi.dev/v1"


def parse_player_from_query(path: str) -> str:
    parsed = urlparse(path)
    params = parse_qs(parsed.query)
    if "pid" in params and params["pid"]:
        return params["pid"][0]
    return ""


def parse_clan_from_query(path: str) -> str:
    parsed = urlparse(path)
    params = parse_qs(parsed.query)
    if "clan" in params and params["clan"]:
        return params["clan"][0]
    return ""


def normalize_player_tag(raw_tag: str) -> str:
    clean = (raw_tag or "").replace("#", "").replace("%23", "")
    clean = "".join(ch for ch in clean if ch.isalnum())
    return clean.upper()


def parse_created_date_sort_key(created_date: str) -> int:
    stamp = (created_date or "")[:8]
    return int(stamp) if stamp.isdigit() else 0


def find_player_weekend_stats(race: Dict[str, object], clan_tag: str, player_tag: str) -> Tuple[int, int] | None:
    standings = race.get("standings") or []
    clans_blob = race.get("clans") or []
    source_rows: List[Dict[str, object]] = []

    if standings:
        for standing in standings:
            clan = standing.get("clan") or {}
            if normalize_player_tag(str(clan.get("tag") or "")) == clan_tag:
                source_rows = clan.get("participants") or standing.get("participants") or []
                break
    elif clans_blob:
        for clan in clans_blob:
            if normalize_player_tag(str(clan.get("tag") or "")) == clan_tag:
                source_rows = clan.get("participants") or []
                break

    for participant in source_rows:
        participant_tag = normalize_player_tag(str(participant.get("tag") or ""))
        if participant_tag != player_tag:
            continue

        fame = int(participant.get("fame") or 0)
        repair_points = int(participant.get("repairPoints") or 0)
        contribution = fame + repair_points
        decks_used = max(0, min(16, int(participant.get("decksUsed") or 0)))
        return contribution, decks_used

    return None


def api_get(path: str, api_key: str) -> Dict[str, object]:
    response = requests.get(
        f"{ROYAL_API_BASE_URL}{path}",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=25,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Clash API error {response.status_code} for {path}")
    return response.json() if response.content else {}


def collect_clan_war_history_for_player(api_key: str, clan_tag: str, player_tag: str) -> List[Dict[str, object]]:
    encoded_clan_tag = f"%23{clan_tag}"
    race_items = list((api_get(f"/clans/{encoded_clan_tag}/riverracelog", api_key)).get("items", []))

    try:
        current_race = api_get(f"/clans/{encoded_clan_tag}/currentriverrace", api_key)
        if current_race:
            race_items.append(current_race)
    except Exception:
        pass

    race_items.sort(
        key=lambda race: (
            int(race.get("seasonId") or 0),
            parse_created_date_sort_key(str(race.get("createdDate") or "")),
        )
    )

    history: List[Dict[str, object]] = []
    for race in race_items:
        season = int(race.get("seasonId") or 0)
        section = int(race.get("sectionIndex") or 0)
        week_key = f"{season}-{section}"
        stats = find_player_weekend_stats(race, clan_tag, player_tag)
        if not stats:
            continue
        contribution, decks_used = stats
        history.append(
            {
                "clan_tag": clan_tag,
                "week": week_key,
                "contribution": contribution,
                "decks_used": decks_used,
            }
        )

    return history


def extract_recent_clan_tags_from_battlelog(api_key: str, player_tag: str) -> List[str]:
    battlelog = api_get(f"/players/%23{player_tag}/battlelog", api_key)
    if not isinstance(battlelog, list):
        return []

    ordered_tags: List[str] = []
    seen = set()
    for battle in battlelog:
        team = battle.get("team") or []
        if not team:
            continue
        clan = (team[0] or {}).get("clan") or {}
        tag = normalize_player_tag(str(clan.get("tag") or ""))
        if not tag or tag in seen:
            continue
        seen.add(tag)
        ordered_tags.append(tag)

    return ordered_tags


def fetch_player_summary(pid: str, clan_tag: str) -> dict:
    api_key = os.environ.get("CLASH_ROYALE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing CLASH_ROYALE_API_KEY environment variable.")

    clean_pid = normalize_player_tag(pid)
    if not clean_pid:
        raise RuntimeError("Missing or invalid player tag.")

    clan_config = get_clan_config(clan_tag or DEFAULT_CLAN_TAG)
    norm_clan_tag = clan_config["tag"]

    data = api_get(f"/players/%23{clean_pid}", api_key)

    recent_clan_tags = extract_recent_clan_tags_from_battlelog(api_key, clean_pid)
    all_clan_tags: List[str] = [norm_clan_tag]
    for tag in recent_clan_tags:
        if tag not in all_clan_tags:
            all_clan_tags.append(tag)

    war_history = collect_clan_war_history_for_player(api_key, norm_clan_tag, clean_pid)

    total_war_history: List[Dict[str, object]] = []
    for tag in all_clan_tags:
        try:
            total_war_history.extend(collect_clan_war_history_for_player(api_key, tag, clean_pid))
        except Exception:
            # Keep response resilient even when a discovered clan cannot be queried.
            continue

    total_war_history.sort(
        key=lambda row: (
            int(str(row.get("week") or "0-0").split("-")[0] or 0),
            int(str(row.get("week") or "0-0").split("-")[1] or 0),
        )
    )

    return {
        "pid": clean_pid,
        "acc_lvl": str(data.get("expLevel") or "-"),
        "cw2_wins": str(data.get("warDayWins") or 0),
        "url": f"https://royaleapi.com/player/{clean_pid}",
        "clan_tag": norm_clan_tag,
        "war_history": war_history,
        "total_war_history": total_war_history,
        "history_clan_tags": all_clan_tags,
        "history_note": "Total history is assembled from clans seen in player's recent battle log plus selected clan.",
    }


def classify_error(exc: Exception) -> tuple[int, str]:
    message = str(exc)
    lower = message.lower()

    if "missing or invalid player tag" in lower:
        return 400, message

    if "missing clash_royale_api_key" in lower:
        return 500, message

    if "clash api error" in lower:
        return 502, "Official Clash API request failed. Try again shortly."

    if "httpsconnectionpool" in lower or "network" in lower or "proxy" in lower:
        return 502, "Network/proxy error while contacting official Clash API. Retry in a moment."

    return 500, message


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            pid = parse_player_from_query(self.path)
            clan_tag = parse_clan_from_query(self.path)
            data = fetch_player_summary(pid, clan_tag=clan_tag)

            payload = {
                "ok": True,
                "player": data,
            }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        except Exception as exc:
            status_code, friendly_message = classify_error(exc)
            payload = {
                "ok": False,
                "error": friendly_message,
                "details": str(exc),
            }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
