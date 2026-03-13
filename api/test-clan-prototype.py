from http.server import BaseHTTPRequestHandler
import json
import os
from urllib.parse import parse_qs, urlparse

import requests


ROYAL_API_BASE_URL = "https://proxy.royaleapi.dev/v1"
DEFAULT_CLAN_TAG = "9YP8UY"
ALLOWED_CLANS = {"9YP8UY", "GPCLVLPP"}


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
            participant_rows = race_data.get("clan", {}).get("participants", []) if isinstance(race_data, dict) else []
            participant_by_tag = {
                str(item.get("tag", "")).replace("#", ""): item
                for item in participant_rows
                if item.get("tag")
            }

            players = []
            for member in members:
                clean_tag = str(member.get("tag", "")).replace("#", "")
                participant = participant_by_tag.get(clean_tag, {})
                players.append(
                    {
                        "name": member.get("name", ""),
                        "tag": member.get("tag", ""),
                        "role": member.get("role", ""),
                        "trophies": member.get("trophies", 0),
                        "fame": participant.get("fame", 0),
                        "repair_points": participant.get("repairPoints", 0),
                        "boat_attacks": participant.get("boatAttacks", 0),
                        "decks_used_today": participant.get("decksUsedToday", 0),
                    }
                )

            players.sort(key=lambda p: (p.get("fame", 0), p.get("decks_used_today", 0)), reverse=True)

            clan_war = race_data.get("clan", {}) if isinstance(race_data, dict) else {}
            opponents = race_data.get("clans", []) if isinstance(race_data, dict) else []

            overview_rows = []
            for row in opponents:
                participants = row.get("participants", []) or []
                decks_today = sum(int(p.get("decksUsedToday") or 0) for p in participants)
                medals = int(row.get("fame") or 0)
                boat_points = int(row.get("repairPoints") or 0)
                total_score = medals + boat_points
                avg_per_deck = round(total_score / decks_today, 2) if decks_today else 0.0
                projected_total = int(round(avg_per_deck * 200))

                overview_rows.append(
                    {
                        "name": row.get("name", "-"),
                        "tag": row.get("tag", "-"),
                        "decks_today": decks_today,
                        "avg_per_deck": avg_per_deck,
                        "projected_total": projected_total,
                        "boat_points": boat_points,
                        "medals": medals,
                        "total_score": total_score,
                    }
                )

            overview_rows.sort(key=lambda r: r.get("total_score", 0), reverse=True)

            is_open = str(clan_data.get("type", "")).lower() == "open"

            own_decks = sum(int(p.get("decksUsedToday") or 0) for p in participant_rows)
            own_total = int(clan_war.get("fame") or 0) + int(clan_war.get("repairPoints") or 0)
            own_avg = round(own_total / own_decks, 2) if own_decks else 0.0
            own_projected = int(round(own_avg * 200))

            return self._send_json(
                200,
                {
                    "ok": True,
                    "clan_tag": clan_tag,
                    "clan": {
                        "name": clan_data.get("name", ""),
                        "tag": clan_data.get("tag", ""),
                        "type": clan_data.get("type", ""),
                        "members": clan_data.get("members", 0),
                        "clan_score": clan_data.get("clanScore", 0),
                        "war_trophies": clan_data.get("clanWarTrophies", 0),
                        "description": clan_data.get("description", ""),
                    },
                    "race_state": {
                        "section_index": race_data.get("sectionIndex"),
                        "period_index": race_data.get("periodIndex"),
                        "medals": int(clan_war.get("fame") or 0),
                        "boat_points": int(clan_war.get("repairPoints") or 0),
                        "participants": len(participant_rows),
                        "decks_used_today": sum(int(p.get("decksUsedToday") or 0) for p in participant_rows),
                        # Calculated here for prototype parity: total score / decks today.
                        "total_score": int(clan_war.get("fame") or 0) + int(clan_war.get("repairPoints") or 0),
                        "avg_per_deck": own_avg,
                        "projected_total": own_projected,
                    },
                    "overview_rows": overview_rows,
                    "players": players,
                    "is_open_clan": is_open,
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
