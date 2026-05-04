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
    battle_count = int(data.get("battleCount") or 0)
    wins = int(data.get("wins") or 0)
    losses = int(data.get("losses") or 0)
    win_rate = round((wins / (wins + losses)) * 100, 1) if (wins + losses) else None

    clan = data.get("clan") or {}
    clan_name = clan.get("name") or "-"
    clan_role = data.get("role") or "-"

    return {
        "pid": clean_pid,
        "name": data.get("name") or clean_pid,
        "acc_lvl": str(data.get("expLevel") or "-"),
        "trophies": int(data.get("trophies") or 0),
        "best_trophies": int(data.get("bestTrophies") or 0),
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "battle_count": battle_count,
        "clan_name": clan_name,
        "clan_role": clan_role,
        "donations": int(data.get("donations") or 0),
        "donations_received": int(data.get("donationsReceived") or 0),
        "league": ((data.get("leagueStatistics") or {}).get("currentSeason") or {}).get("trophies"),
        "cw2_wins": str(data.get("warDayWins") or 0),
        "cw2_wins_note": "Official API field `warDayWins` (can differ from RoyaleAPI lifetime CW2 interpretation).",
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
