from http.server import BaseHTTPRequestHandler
import json
import os
from urllib.parse import parse_qs, urlparse

try:
    from api.clash_client import ClashClientError, ClashRoyaleClient, normalize_tag
except ImportError:  # pragma: no cover - useful when loaded as a loose file.
    from clash_client import ClashClientError, ClashRoyaleClient, normalize_tag


def parse_player_from_query(path: str) -> str:
    parsed = urlparse(path)
    params = parse_qs(parsed.query)
    if "pid" in params and params["pid"]:
        return params["pid"][0]
    return ""


def normalize_player_tag(raw_tag: str) -> str:
    try:
        return normalize_tag(raw_tag)
    except ClashClientError:
        return ""


def fetch_player_summary(pid: str, *, client=None) -> dict:
    api_key = os.environ.get("CLASH_ROYALE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing CLASH_ROYALE_API_KEY environment variable.")

    clean_pid = normalize_player_tag(pid)
    if not clean_pid:
        raise RuntimeError("Missing or invalid player tag.")

    official_client = client or ClashRoyaleClient(api_key=api_key)
    response = official_client.get_player(clean_pid)
    data = response.data if isinstance(response.data, dict) else {}
    return {
        "pid": clean_pid,
        "acc_lvl": str(data.get("expLevel") or "-"),
        "cw2_wins": str(data.get("warDayWins") or 0),
        "url": f"https://royaleapi.com/player/{clean_pid}",
        "source": response.source,
        "fetched_at": response.fetched_at,
    }


def classify_error(exc: Exception) -> tuple[int, str]:
    message = str(exc)
    lower = message.lower()

    if "missing or invalid player tag" in lower:
        return 400, message

    if "missing clash_royale_api_key" in lower:
        return 500, message

    if isinstance(exc, ClashClientError) or "clash api error" in lower:
        return 502, "Official Clash API request failed. Try again shortly."

    if "httpsconnectionpool" in lower or "network" in lower or "proxy" in lower:
        return 502, "Network/proxy error while contacting official Clash API. Retry in a moment."

    return 500, "Player summary is temporarily unavailable."


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
            }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
