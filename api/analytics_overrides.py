from http.server import BaseHTTPRequestHandler
import json

import requests

from Royale_api import get_clan_config
from supabase_history import (
    EXCLUSIONS_TABLE,
    _supabase_headers,
    clash_date_to_iso,
    get_supabase_read_config,
    normalize_tag,
)


MAX_BODY_BYTES = 4096
MAX_REASON_LENGTH = 240


def validate_payload(payload: dict) -> dict:
    clan_tag = normalize_tag(payload.get("clan_tag"))
    player_tag = normalize_tag(payload.get("player_tag"))
    if not clan_tag or not player_tag:
        raise ValueError("Clan tag en player tag zijn verplicht.")

    clan_config = get_clan_config(clan_tag)
    clan_tag = normalize_tag(clan_config.get("tag"))
    race_created_at = clash_date_to_iso(payload.get("race_created_at"))
    excluded = payload.get("excluded")
    if not isinstance(excluded, bool):
        raise ValueError("excluded moet true of false zijn.")

    reason = str(payload.get("reason") or "").strip()
    if len(reason) > MAX_REASON_LENGTH:
        raise ValueError(
            f"Reden mag maximaal {MAX_REASON_LENGTH} tekens bevatten."
        )
    if excluded and not reason:
        reason = "Handmatig uitgesloten"

    return {
        "clan_tag": clan_tag,
        "race_created_at": race_created_at,
        "player_tag": player_tag,
        "excluded": excluded,
        "reason": reason,
    }


def update_override(payload: dict, admin_key: str) -> dict:
    if not admin_key:
        raise PermissionError("Beheerkey ontbreekt.")

    config = get_supabase_read_config()
    if not config:
        raise RuntimeError("Supabase is niet geconfigureerd.")
    supabase_url, publishable_key = config
    endpoint = f"{supabase_url.rstrip('/')}/rest/v1/{EXCLUSIONS_TABLE}"
    headers = {
        **_supabase_headers(publishable_key, write=True),
        "X-Analytics-Admin-Key": admin_key,
    }

    record = {
        "clan_tag": payload["clan_tag"],
        "race_created_at": payload["race_created_at"],
        "player_tag": payload["player_tag"],
        "reason": payload["reason"],
    }
    if payload["excluded"]:
        response = requests.post(
            (
                f"{endpoint}?on_conflict="
                "clan_tag,race_created_at,player_tag"
            ),
            json=record,
            headers=headers,
            timeout=25,
        )
        action = "excluded"
    else:
        response = requests.delete(
            endpoint,
            params={
                "clan_tag": f"eq.{payload['clan_tag']}",
                "race_created_at": f"eq.{payload['race_created_at']}",
                "player_tag": f"eq.{payload['player_tag']}",
            },
            headers=headers,
            timeout=25,
        )
        action = "included"

    if response.status_code in (401, 403):
        raise PermissionError("Beheerkey is onjuist.")
    if response.status_code not in (200, 201, 204):
        raise RuntimeError(
            "Opslaan van de weekuitzondering mislukte "
            f"(HTTP {response.status_code})."
        )
    return {"action": action, **record}


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            content_length = int(self.headers.get("Content-Length") or 0)
            if content_length <= 0 or content_length > MAX_BODY_BYTES:
                raise ValueError("Ongeldige requestgrootte.")
            payload = json.loads(self.rfile.read(content_length) or b"{}")
            if not isinstance(payload, dict):
                raise ValueError("Request moet een JSON-object zijn.")
            validated = validate_payload(payload)
            result = update_override(
                validated,
                self.headers.get("X-Analytics-Admin-Key", "").strip(),
            )
            status = 200
            response_payload = {"ok": True, **result}
        except PermissionError as exc:
            status = 403
            response_payload = {"ok": False, "error": str(exc)}
        except (ValueError, json.JSONDecodeError) as exc:
            status = 400
            response_payload = {"ok": False, "error": str(exc)}
        except Exception as exc:
            status = 500
            response_payload = {"ok": False, "error": str(exc)}

        body = json.dumps(response_payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

