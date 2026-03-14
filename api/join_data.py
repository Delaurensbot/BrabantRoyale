from http.server import BaseHTTPRequestHandler
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from urllib.parse import parse_qs, urlparse

import requests

from Royale_api import DEFAULT_CLAN_TAG, get_clan_config

ROYAL_API_BASE_URL = "https://proxy.royaleapi.dev/v1"
SNAPSHOT_DIR = Path(os.environ.get("JOIN_TRACKER_DIR") or (Path(tempfile.gettempdir()) / "join_tracker"))


def parse_limit_from_query(path: str) -> int:
    parsed = urlparse(path)
    params = parse_qs(parsed.query)
    if "limit" in params and params["limit"]:
        try:
            return int(params["limit"][0])
        except (TypeError, ValueError):
            return 10
    return 10


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


def api_get(path: str, api_key: str) -> dict:
    url = f"{ROYAL_API_BASE_URL}{path}"
    response = requests.get(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=25,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Clash API error {response.status_code} for {path}")
    return response.json() if response.content else {}


def snapshot_path(clan_tag: str) -> Path | None:
    try:
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        return SNAPSHOT_DIR / f"{clan_tag}.json"
    except OSError:
        return None


def load_snapshot(clan_tag: str) -> dict:
    path = snapshot_path(clan_tag)
    if path is None or not path.exists():
        return {"members": [], "joins": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"members": [], "joins": []}


def save_snapshot(clan_tag: str, payload: dict) -> bool:
    path = snapshot_path(clan_tag)
    if path is None:
        return False
    try:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return True
    except OSError:
        return False


def collect_join_data_official(limit: int, clan_tag: str) -> dict:
    api_key = os.environ.get("CLASH_ROYALE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing CLASH_ROYALE_API_KEY environment variable.")

    clan_config = get_clan_config(clan_tag)
    norm_clan_tag = clan_config["tag"]
    encoded = f"%23{norm_clan_tag}"

    members_payload = api_get(f"/clans/{encoded}/members", api_key)
    member_items = members_payload.get("items", [])

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    current_map = {}
    for member in member_items:
        tag = normalize_player_tag(str(member.get("tag") or ""))
        if not tag:
            continue
        current_map[tag] = {
            "tag": tag,
            "name": member.get("name") or tag,
            "fetched_at": now_utc,
            "url": f"https://royaleapi.com/player/{tag}",
        }

    snapshot = load_snapshot(norm_clan_tag)
    previous_members = {normalize_player_tag(str(t)): True for t in snapshot.get("members", [])}
    joins = snapshot.get("joins", [])

    for tag, info in current_map.items():
        if tag not in previous_members:
            joins.append(
                {
                    "name": info["name"],
                    "pid": tag,
                    "ago": "just now",
                    "utc": now_utc,
                    "url": info["url"],
                }
            )

    # Keep only latest 200 stored join events.
    joins = joins[-200:]

    persisted = save_snapshot(
        norm_clan_tag,
        {
            "members": sorted(current_map.keys()),
            "joins": joins,
            "updated_at": now_utc,
        },
    )

    note = "Join history is tracked from local snapshots using official API member diffs."
    if not persisted:
        note = "Snapshot storage is unavailable (read-only FS). Showing only joins detected during this runtime."

    return {
        "fetched_at": now_utc,
        "source_url": f"{ROYAL_API_BASE_URL}/clans/%23{norm_clan_tag}/members",
        "clan_tag": norm_clan_tag,
        "clan_name": clan_config.get("name"),
        "joins": list(reversed(joins[-limit:])),
        "note": note,
    }


def classify_error(exc: Exception) -> tuple[int, str]:
    message = str(exc)
    lower = message.lower()

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
            limit = max(1, min(50, parse_limit_from_query(self.path)))
            clan_tag = parse_clan_from_query(self.path)
            data = collect_join_data_official(limit=limit, clan_tag=clan_tag or DEFAULT_CLAN_TAG)

            payload = {
                "ok": True,
                "fetched_at": data["fetched_at"],
                "source_url": data["source_url"],
                "clan_tag": data.get("clan_tag"),
                "clan_name": data.get("clan_name"),
                "limit": limit,
                "joins": data["joins"],
                "note": data.get("note"),
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
