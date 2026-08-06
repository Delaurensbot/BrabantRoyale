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


def fetch_player_summary(pid: str) -> dict:
    api_key = os.environ.get("CLASH_ROYALE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing CLASH_ROYALE_API_KEY environment variable.")

    clean_pid = normalize_player_tag(pid)
    if not clean_pid:
        raise RuntimeError("Missing or invalid player tag.")

    endpoint = f"{ROYAL_API_BASE_URL}/players/%23{clean_pid}"
    response = requests.get(
        endpoint,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=25,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Clash API error {response.status_code} for /players/%23{clean_pid}")

    data = response.json() if response.content else {}
    cards = data.get("cards") or []
    level_15_cards = 0
    level_16_cards = 0

    for card in cards:
        display_level = card.get("level") or 0
        max_level = card.get("maxLevel")
        elite_level = card.get("eliteLevel")

        try:
            display_level = int(display_level)
        except Exception:
            display_level = 0

        try:
            max_level_int = int(max_level)
            display_level = display_level + (14 - max_level_int)
        except Exception:
            pass

        try:
            elite_level_int = int(elite_level)
            if elite_level_int > 0:
                display_level = 15 + elite_level_int
        except Exception:
            pass

        if display_level == 15:
            level_15_cards += 1
        elif display_level >= 16:
            level_16_cards += 1

    history = []
    history_endpoint = f"{ROYAL_API_BASE_URL}/players/%23{clean_pid}/history"
    history_response = requests.get(
        history_endpoint,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=25,
    )

    if history_response.status_code == 200:
        history_data = history_response.json() if history_response.content else {}
        events = history_data if isinstance(history_data, list) else history_data.get("items") or []

        for item in events:
            clan = item.get("clan") or {}
            tag = normalize_player_tag(str(clan.get("tag") or item.get("tag") or ""))
            name = str(clan.get("name") or item.get("name") or "").strip()
            joined_at = item.get("startTime") or item.get("joined") or item.get("joinedAt") or ""
            left_at = item.get("endTime") or item.get("left") or item.get("leftAt") or ""
            if not tag and not name:
                continue
            history.append(
                {
                    "tag": tag,
                    "name": name,
                    "joined_at": joined_at,
                    "left_at": left_at,
                }
            )

    seen = set()
    unique_history = []
    for row in history:
        key = (row.get("tag"), row.get("name"), row.get("joined_at"), row.get("left_at"))
        if key in seen:
            continue
        seen.add(key)
        unique_history.append(row)

    return {
        "pid": clean_pid,
        "acc_lvl": str(data.get("expLevel") or "-"),
        "cw2_wins": str(data.get("warDayWins") or 0),
        "cards_lvl_15": level_15_cards,
        "cards_lvl_16": level_16_cards,
        "clan_history": unique_history,
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
