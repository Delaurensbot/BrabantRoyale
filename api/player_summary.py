from datetime import datetime, timezone
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


def _safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def _parse_battle_time(value: str):
    if not value:
        return None

    text = str(value).strip()
    # Clash format example: 20240215T183025.000Z
    for fmt in ("%Y%m%dT%H%M%S.%fZ", "%Y%m%dT%H%M%SZ"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except Exception:
            continue

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None


def _weekend_key_and_label(dt: datetime):
    # ISO year/week for grouping into war-weekend snapshots.
    iso_year, iso_week, _ = dt.isocalendar()
    return f"{iso_year}-W{iso_week:02d}", f"Week {iso_week} ({iso_year})"


def _extract_clan_history_items(raw_history) -> list[dict]:
    """Parse clan history from multiple possible payload shapes."""
    if isinstance(raw_history, list):
        candidates = raw_history
    elif isinstance(raw_history, dict):
        if isinstance(raw_history.get("items"), list):
            candidates = raw_history.get("items") or []
        elif isinstance(raw_history.get("history"), list):
            candidates = raw_history.get("history") or []
        elif isinstance(raw_history.get("clans"), list):
            candidates = raw_history.get("clans") or []
        else:
            candidates = []
    else:
        candidates = []

    parsed = []
    for item in candidates:
        if not isinstance(item, dict):
            continue

        clan = item.get("clan") if isinstance(item.get("clan"), dict) else {}
        tag = normalize_player_tag(str(clan.get("tag") or item.get("tag") or ""))
        name = str(clan.get("name") or item.get("name") or "").strip()
        joined_at = item.get("startTime") or item.get("joined") or item.get("joinedAt") or ""
        left_at = item.get("endTime") or item.get("left") or item.get("leftAt") or ""

        if tag or name:
            parsed.append(
                {
                    "tag": tag,
                    "name": name,
                    "joined_at": str(joined_at or ""),
                    "left_at": str(left_at or ""),
                    "source": "history_endpoint",
                }
            )

        nested = item.get("history")
        if isinstance(nested, list):
            for sub in nested:
                if not isinstance(sub, dict):
                    continue
                sub_tag = normalize_player_tag(str(sub.get("tag") or ""))
                sub_name = str(sub.get("name") or "").strip()
                sub_joined = sub.get("startTime") or sub.get("joined") or sub.get("joinedAt") or ""
                sub_left = sub.get("endTime") or sub.get("left") or sub.get("leftAt") or ""
                if sub_tag or sub_name:
                    parsed.append(
                        {
                            "tag": sub_tag,
                            "name": sub_name,
                            "joined_at": str(sub_joined or ""),
                            "left_at": str(sub_left or ""),
                            "source": "history_endpoint",
                        }
                    )

    seen = set()
    unique = []
    for row in parsed:
        key = (row.get("tag"), row.get("name"), row.get("joined_at"), row.get("left_at"), row.get("source"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)

    return unique


def _extract_war_weekend_clans(battlelog) -> list[dict]:
    """Infer clan snapshots per war-week from battlelog entries."""
    if not isinstance(battlelog, list):
        return []

    weekend_rows = {}

    for battle in battlelog:
        if not isinstance(battle, dict):
            continue

        battle_type = str(battle.get("type") or "").lower()
        game_mode = battle.get("gameMode") if isinstance(battle.get("gameMode"), dict) else {}
        game_mode_name = str(game_mode.get("name") or "").lower()
        hint = f"{battle_type} {game_mode_name}"

        is_war_like = any(token in hint for token in ["river", "boat", "war"])
        if not is_war_like:
            continue

        battle_time = _parse_battle_time(str(battle.get("battleTime") or ""))
        if not battle_time:
            continue

        key, label = _weekend_key_and_label(battle_time)

        candidates = []
        for field in ("clan",):
            val = battle.get(field)
            if isinstance(val, dict):
                candidates.append(val)

        team = battle.get("team")
        if isinstance(team, list) and team:
            first = team[0] if isinstance(team[0], dict) else {}
            first_clan = first.get("clan") if isinstance(first.get("clan"), dict) else {}
            if first_clan:
                candidates.append(first_clan)

        clan_tag = ""
        clan_name = ""
        for clan in candidates:
            maybe_tag = normalize_player_tag(str(clan.get("tag") or ""))
            maybe_name = str(clan.get("name") or "").strip()
            if maybe_tag or maybe_name:
                clan_tag = maybe_tag
                clan_name = maybe_name
                break

        if not clan_tag and not clan_name:
            continue

        existing = weekend_rows.get(key)
        if not existing or battle_time > existing.get("_dt"):
            weekend_rows[key] = {
                "week_key": key,
                "week_label": label,
                "tag": clan_tag,
                "name": clan_name,
                "battle_time": battle_time.strftime("%Y-%m-%d %H:%M UTC"),
                "source": "war_battlelog",
                "_dt": battle_time,
            }

    rows = list(weekend_rows.values())
    rows.sort(key=lambda r: r.get("week_key", ""), reverse=True)

    for row in rows:
        row.pop("_dt", None)

    return rows


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
        display_level = _safe_int(card.get("level"), 0)
        max_level = _safe_int(card.get("maxLevel"), 0)
        elite_level = _safe_int(card.get("eliteLevel"), 0)

        if max_level > 0:
            display_level = display_level + (14 - max_level)
        if elite_level > 0:
            display_level = 15 + elite_level

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
        history = _extract_clan_history_items(history_data)

    battlelog_endpoint = f"{ROYAL_API_BASE_URL}/players/%23{clean_pid}/battlelog"
    battlelog_response = requests.get(
        battlelog_endpoint,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=25,
    )
    war_weekend_history = []
    if battlelog_response.status_code == 200:
        battlelog_data = battlelog_response.json() if battlelog_response.content else []
        war_weekend_history = _extract_war_weekend_clans(battlelog_data)

    # Enrich clan history with war-week snapshots if tags/names are not already present.
    for row in war_weekend_history:
        tag = row.get("tag") or ""
        name = row.get("name") or ""
        exists = any((h.get("tag") == tag and h.get("name") == name) for h in history)
        if not exists and (tag or name):
            history.append(
                {
                    "tag": tag,
                    "name": name,
                    "joined_at": "",
                    "left_at": "",
                    "source": "war_battlelog",
                }
            )

    current_clan = data.get("clan") if isinstance(data.get("clan"), dict) else {}
    if not history and (current_clan.get("tag") or current_clan.get("name")):
        history = [
            {
                "tag": normalize_player_tag(str(current_clan.get("tag") or "")),
                "name": str(current_clan.get("name") or "").strip(),
                "joined_at": "",
                "left_at": "",
                "source": "current_profile",
            }
        ]

    return {
        "pid": clean_pid,
        "name": str(data.get("name") or "-").strip() or "-",
        "acc_lvl": str(data.get("expLevel") or "-"),
        "cw2_wins": str(data.get("warDayWins") or 0),
        "cards_lvl_15": level_15_cards,
        "cards_lvl_16": level_16_cards,
        "clan_history": history,
        "war_weekend_history": war_weekend_history,
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
