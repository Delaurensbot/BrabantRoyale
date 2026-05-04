from http.server import BaseHTTPRequestHandler
import json
import os
from urllib.parse import parse_qs, urlparse

import requests

ROYAL_API_BASE_URL = "https://proxy.royaleapi.dev/v1"


def parse_player_from_query(path: str) -> str:
    parsed = urlparse(path)
    params = parse_qs(parsed.query)
    if "pid" in params and params["pid"]:
        return params["pid"][0]
    return ""


def normalize_player_tag(raw_tag: str) -> str:
    clean = (raw_tag or "").replace("#", "").replace("%23", "")
    clean = "".join(ch for ch in clean if ch.isalnum())
    return clean.upper()


def api_get(path: str, api_key: str) -> dict:
    endpoint = f"{ROYAL_API_BASE_URL}{path}"
    response = requests.get(
        endpoint,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=25,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Clash API error {response.status_code} for {path}")
    return response.json() if response.content else {}


def count_card_levels(cards: list[dict]) -> tuple[int, int]:
    level_15 = 0
    level_16 = 0
    for card in cards:
        level = int(card.get("level") or 0)
        if level == 15:
            level_15 += 1
        if level == 16:
            level_16 += 1
    return level_15, level_16


def participant_week_from_payload(participant: dict, week_label: str, source: str) -> dict:
    fame = int(participant.get("fame") or 0)
    repair_points = int(participant.get("repairPoints") or 0)
    decks_used = int(participant.get("decksUsed") or 0)
    return {
        "week_label": week_label,
        "source": source,
        "fame": fame,
        "repair_points": repair_points,
        "contribution": fame + repair_points,
        "decks_used": decks_used,
        "perfect_week": decks_used == 16,
    }


def collect_war_weeks_from_clan(clan_tag: str, player_tag: str, api_key: str) -> list[dict]:
    encoded = f"%23{clan_tag}"
    observed: list[dict] = []

    current = api_get(f"/clans/{encoded}/currentriverrace", api_key)
    section_index = current.get("sectionIndex")
    current_label = f"current section {section_index}" if section_index is not None else "current river race"
    for clan in current.get("clans", []) or []:
        if normalize_player_tag(str(clan.get("tag") or "")) != clan_tag:
            continue
        for participant in clan.get("participants", []) or []:
            if normalize_player_tag(str(participant.get("tag") or "")) == player_tag:
                observed.append(participant_week_from_payload(participant, current_label, "currentriverrace"))
                break
        break

    race_log = api_get(f"/clans/{encoded}/riverracelog", api_key)
    for race in race_log.get("items", []) or []:
        season_id = race.get("seasonId")
        sec = race.get("sectionIndex")
        created = race.get("createdDate")
        week_label = f"season {season_id}, section {sec}" if season_id is not None and sec is not None else (created or "historical week")

        for standing in race.get("standings", []) or []:
            standing_clan = normalize_player_tag(str((standing.get("clan") or {}).get("tag") or ""))
            if standing_clan != clan_tag:
                continue

            participants = standing.get("participants", []) or []
            for participant in participants:
                if normalize_player_tag(str(participant.get("tag") or "")) == player_tag:
                    observed.append(participant_week_from_payload(participant, week_label, "riverracelog"))
                    break
            break

    return observed


def calculate_recent_war_form(observed_weeks: list[dict]) -> dict:
    observed_war_weeks = len(observed_weeks)

    if observed_war_weeks == 0:
        return {
            "has_data": False,
            "message": "No recent clan war data observable from live API",
            "observed_war_weeks": 0,
            "confidence_label": "Low confidence",
            "weeks": [],
        }

    total_contribution = sum(week["contribution"] for week in observed_weeks)
    total_decks_used = sum(week["decks_used"] for week in observed_weeks)
    weeks_above_3000 = sum(1 for week in observed_weeks if week["contribution"] >= 3000)

    avg_contribution = round(total_contribution / observed_war_weeks, 1)
    avg_decks_used = round(total_decks_used / observed_war_weeks, 2)
    reliability_pct = round((total_decks_used / (observed_war_weeks * 16)) * 100, 1)

    longest_recent_perfect_streak = 0
    streak = 0
    for week in observed_weeks:
        if week["decks_used"] == 16:
            streak += 1
            longest_recent_perfect_streak = max(longest_recent_perfect_streak, streak)
        else:
            streak = 0

    current_perfect_streak = 0
    for week in observed_weeks:
        if week["decks_used"] == 16:
            current_perfect_streak += 1
        else:
            break

    if observed_war_weeks >= 5:
        confidence_label = "High confidence"
    elif observed_war_weeks >= 2:
        confidence_label = "Medium confidence"
    else:
        confidence_label = "Low confidence"

    return {
        "has_data": True,
        "message": None,
        "observed_war_weeks": observed_war_weeks,
        "avg_contribution": avg_contribution,
        "weeks_above_3000": weeks_above_3000,
        "total_decks_used": total_decks_used,
        "avg_decks_used": avg_decks_used,
        "reliability_pct": reliability_pct,
        "longest_recent_perfect_streak": longest_recent_perfect_streak,
        "current_perfect_streak": current_perfect_streak,
        "confidence_label": confidence_label,
        "weeks": observed_weeks,
    }


def fetch_player_summary(pid: str) -> dict:
    api_key = os.environ.get("CLASH_ROYALE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing CLASH_ROYALE_API_KEY environment variable.")

    clean_pid = normalize_player_tag(pid)
    if not clean_pid:
        raise RuntimeError("Missing or invalid player tag.")

    player_data = api_get(f"/players/%23{clean_pid}", api_key)

    cards = player_data.get("cards") or []
    level_15_cards, level_16_cards = count_card_levels(cards)

    clan = player_data.get("clan") or {}
    clan_tag = normalize_player_tag(str(clan.get("tag") or ""))
    clan_name = clan.get("name") or "-"

    observed_weeks = []
    if clan_tag:
        observed_weeks = collect_war_weeks_from_clan(clan_tag=clan_tag, player_tag=clean_pid, api_key=api_key)

    recent_war_form = calculate_recent_war_form(observed_weeks)

    return {
        "pid": clean_pid,
        "name": player_data.get("name") or clean_pid,
        "acc_lvl": str(player_data.get("expLevel") or "-"),
        "trophies": int(player_data.get("trophies") or 0),
        "best_trophies": int(player_data.get("bestTrophies") or 0),
        "clan_name": clan_name,
        "clan_tag": clan_tag or None,
        "cards_total": len(cards),
        "level_15_cards": level_15_cards,
        "level_16_cards": level_16_cards,
        "recent_war_form": recent_war_form,
        "data_sources": {
            "live_endpoints": [
                f"{ROYAL_API_BASE_URL}/players/%23{clean_pid}",
                f"{ROYAL_API_BASE_URL}/clans/%23{clan_tag}/riverracelog" if clan_tag else None,
                f"{ROYAL_API_BASE_URL}/clans/%23{clan_tag}/currentriverrace" if clan_tag else None,
            ],
            "storage": "none",
        },
        "url": f"https://royaleapi.com/player/{clean_pid}",
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
            data = fetch_player_summary(pid)

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
