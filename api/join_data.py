from http.server import BaseHTTPRequestHandler
import json
from urllib.parse import urlparse, parse_qs

from Royale_api_join_data import collect_join_data


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


def classify_error(exc: Exception) -> tuple[int, str]:
    message = str(exc)
    lower = message.lower()

    if "blocked by anti-bot" in lower or "cloudflare" in lower or "captcha" in lower:
        return 502, "RoyaleAPI blocked this request (Cloudflare/anti-bot). Try again shortly."

    if "network failure while fetching" in lower or "httpsconnectionpool" in lower or "proxy" in lower:
        return 502, "Network/proxy error while contacting RoyaleAPI. Retry in a moment."

    if "failed to fetch join-leave page" in lower:
        return 502, message

    return 500, message


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            limit = parse_limit_from_query(self.path)
            clan_tag = parse_clan_from_query(self.path)
            data = collect_join_data(limit=limit, clan_tag=clan_tag)

            payload = {
                "ok": True,
                "fetched_at": data["fetched_at"],
                "source_url": data["source_url"],
                "clan_tag": data.get("clan_tag"),
                "clan_name": data.get("clan_name"),
                "limit": limit,
                "joins": data["joins"],
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
