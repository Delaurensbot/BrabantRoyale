from http.server import BaseHTTPRequestHandler
import json
import os
from urllib.parse import parse_qs, urlparse

import requests


ROYAL_API_BASE_URL = "https://proxy.royaleapi.dev/v1"
DEFAULT_CLAN_TAG = "9YP8UY"
ALLOWED_CLANS = {"9YP8UY", "GPCLVLPP", "RLQQQC99"}
MAX_DECKS_PER_PLAYER = 4
MAX_CLAN_DECKS_PER_DAY = 200
COLOSSEUM_SECTION_INDEX = 4
COLOSSEUM_PROJECTION_DAYS = 4


def normalize_tag(raw_tag: str) -> str:
    cleaned = (raw_tag or "").replace("#", "").replace("%23", "")
    cleaned = "".join(ch for ch in cleaned if ch.isalnum())
    normalized = cleaned.upper()
    return normalized if normalized in ALLOWED_CLANS else DEFAULT_CLAN_TAG


def request_json(endpoint: str, api_key: str):
    response = requests.get(
        endpoint,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=20,
    )
    response.raise_for_status()
    return response.json() if response.content else {}


def int_value(value, default=0):
    try:
        return int(value or 0)
    except Exception:
        return default


def is_colosseum_race(race_data):
    return int_value((race_data or {}).get("sectionIndex"), -1) == COLOSSEUM_SECTION_INDEX


def projection_multiplier_for_race(race_data):
    return COLOSSEUM_PROJECTION_DAYS if is_colosseum_race(race_data) else 1


def build_overview_rows(clans, projection_multiplier=1):
    projection_multiplier = max(1, int_value(projection_multiplier, 1))
    rows = []
    for row in clans:
        participants = row.get("participants", []) or []
        decks_used = sum(int_value(p.get("decksUsedToday")) for p in participants)
        decks_total = MAX_CLAN_DECKS_PER_DAY
        decks_remaining = max(0, decks_total - decks_used)

        fame = int_value(row.get("fame"))
        repair = int_value(row.get("repairPoints"))
        medals = fame + repair

        avg_per_deck = round((medals / decks_used), 2) if decks_used > 0 else None
        daily_projected = int(round(medals + ((avg_per_deck or 0) * decks_remaining)))
        projected = daily_projected * projection_multiplier

        rows.append(
            {
                "name": row.get("name", "-"),
                "tag": row.get("tag", "-"),
                "fame": fame,
                "repair_points": repair,
                "medals": medals,
                "decks_used_today": decks_used,
                "decks_total_today": decks_total,
                "decks_remaining_today": decks_remaining,
                "avg_medals_per_deck": avg_per_deck,
                "daily_projected_medals": daily_projected,
                "projected_medals": projected,
            }
        )

    rows.sort(key=lambda r: (r.get("medals", 0), r.get("projected_medals", 0)), reverse=True)
    return rows


def build_players(member_items, participant_rows):
    participant_by_tag = {
        str(item.get("tag", "")).replace("#", ""): item
        for item in participant_rows
        if item.get("tag")
    }

    players = []
    for member in member_items:
        clean_tag = str(member.get("tag", "")).replace("#", "")
        participant = participant_by_tag.get(clean_tag, {})

        decks_used_today = int_value(participant.get("decksUsedToday"))
        decks_total_so_far = int_value(participant.get("decksUsed"))
        fame = int_value(participant.get("fame"))
        boat_attacks = int_value(participant.get("boatAttacks"))
        attacks_left = max(0, MAX_DECKS_PER_PLAYER - decks_used_today)

        players.append(
            {
                "name": member.get("name", ""),
                "tag": member.get("tag", ""),
                "role": member.get("role", ""),
                "trophies": int_value(member.get("trophies")),
                "fame": fame,
                "boat_attacks": boat_attacks,
                "decks_used_today": decks_used_today,
                "decks_total_so_far": decks_total_so_far,
                "attacks_left_today": attacks_left,
            }
        )

    players.sort(key=lambda p: (p.get("fame", 0), p.get("decks_used_today", 0)), reverse=True)
    return players


def build_finish_outlook(clan_tag, overview_rows, players, projection_multiplier=1):
    ours = None
    for row in overview_rows:
        if str(row.get("tag", "")).replace("#", "") == clan_tag:
            ours = row
            break

    if not ours:
        return {}

    avg_values = [row.get("avg_medals_per_deck") for row in overview_rows if row.get("avg_medals_per_deck") is not None]
    min_avg = min(avg_values) if avg_values else 0
    max_avg = max(avg_values) if avg_values else 0

    current_medals = int_value(ours.get("medals"))
    remaining_decks = int_value(ours.get("decks_remaining_today"))
    projected_finish = int_value(ours.get("projected_medals"))
    projection_multiplier = max(1, int_value(projection_multiplier, 1))
    best_finish = int(round(current_medals + (remaining_decks * max_avg))) * projection_multiplier
    worst_finish = int(round(current_medals + (remaining_decks * min_avg))) * projection_multiplier

    def rank_for(score: int):
        better = sum(
            1
            for row in overview_rows
            if str(row.get("tag", "")).replace("#", "") != clan_tag
            and int_value(row.get("projected_medals")) > score
        )
        return better + 1

    battles_left = sum(int_value(p.get("attacks_left_today")) for p in players)
    duels_left = sum(1 for p in players if int_value(p.get("attacks_left_today")) >= 3)
    total_players_participated = sum(1 for p in players if int_value(p.get("decks_used_today")) >= 1)

    return {
        "battles_left": battles_left,
        "duels_left": duels_left,
        "total_players_participated": total_players_participated,
        "projected_rank": rank_for(projected_finish),
        "projected_finish": projected_finish,
        "best_rank": rank_for(best_finish),
        "best_finish": best_finish,
        "worst_rank": rank_for(worst_finish),
        "worst_finish": worst_finish,
        "model": "official_api_derived",
        "projection_multiplier": projection_multiplier,
        "projection_scope": "colosseum_4_days" if projection_multiplier > 1 else "single_day",
    }


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        api_key = os.environ.get("CLASH_ROYALE_API_KEY")
        if not api_key:
            return self._send_json(
                500,
                {"ok": False, "error": "Missing CLASH_ROYALE_API_KEY environment variable."},
            )

        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        clan_tag = normalize_tag(params.get("clan", [DEFAULT_CLAN_TAG])[0])
        encoded_tag = f"%23{clan_tag}"

        try:
            clan_data = request_json(f"{ROYAL_API_BASE_URL}/clans/{encoded_tag}", api_key)
            member_data = request_json(f"{ROYAL_API_BASE_URL}/clans/{encoded_tag}/members", api_key)
            race_data = request_json(f"{ROYAL_API_BASE_URL}/clans/{encoded_tag}/currentriverrace", api_key)

            members = member_data.get("items", []) if isinstance(member_data, dict) else []
            race_clans = race_data.get("clans", []) if isinstance(race_data, dict) else []
            own_clan = race_data.get("clan", {}) if isinstance(race_data, dict) else {}
            participant_rows = own_clan.get("participants", []) if isinstance(own_clan, dict) else []

            is_colosseum = is_colosseum_race(race_data)
            projection_multiplier = projection_multiplier_for_race(race_data)
            overview_rows = build_overview_rows(race_clans, projection_multiplier)
            players = build_players(members, participant_rows)
            finish_outlook = build_finish_outlook(
                clan_tag,
                overview_rows,
                players,
                projection_multiplier,
            )

            is_open = str(clan_data.get("type", "")).lower() == "open"

            return self._send_json(
                200,
                {
                    "ok": True,
                    "clan_tag": clan_tag,
                    "clan": {
                        "name": clan_data.get("name", ""),
                        "tag": clan_data.get("tag", ""),
                        "type": clan_data.get("type", ""),
                        "members": int_value(clan_data.get("members")),
                        "clan_score": int_value(clan_data.get("clanScore")),
                        "war_trophies": int_value(clan_data.get("clanWarTrophies")),
                        "description": clan_data.get("description", ""),
                    },
                    "race_state": {
                        "section_index": race_data.get("sectionIndex"),
                        "period_index": race_data.get("periodIndex"),
                        "is_colosseum_weekend": is_colosseum,
                        "projection_multiplier": projection_multiplier,
                        "fame": int_value(own_clan.get("fame")),
                        "repair_points": int_value(own_clan.get("repairPoints")),
                        "participants": len(participant_rows),
                        "decks_used_today": sum(int_value(p.get("decksUsedToday")) for p in participant_rows),
                    },
                    "overview_rows": overview_rows,
                    "players": players,
                    "finish_outlook": finish_outlook,
                    "is_open_clan": is_open,
                    "gaps": {
                        "high_fame_day_cards": "not_directly_available",
                        "cwstats_finish_model": "replaced_with_local_estimate",
                        "colosseum_context": "section_index_4",
                    },
                },
            )
        except requests.HTTPError as http_err:
            status = http_err.response.status_code if http_err.response is not None else 502
            return self._send_json(status, {"ok": False, "error": f"Upstream API error ({status})."})
        except requests.RequestException:
            return self._send_json(502, {"ok": False, "error": "Failed to contact RoyaleAPI proxy."})
        except Exception:
            return self._send_json(500, {"ok": False, "error": "Unexpected server error."})

    def _send_json(self, status_code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
