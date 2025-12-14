from http.server import BaseHTTPRequestHandler
import json
from urllib.parse import urlparse, parse_qs

from Royale_api_join_data import collect_join_data, JOIN_LEAVE_URL


def parse_limit_from_query(path: str) -> int:
    parsed = urlparse(path)
    params = parse_qs(parsed.query)
    if "limit" in params and params["limit"]:
        try:
            return int(params["limit"][0])
        except (TypeError, ValueError):
            return 10
    return 10


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            limit = parse_limit_from_query(self.path)
            data = collect_join_data(limit=limit)

            payload = {
                "ok": True,
                "fetched_at": data["fetched_at"],
                "source_url": data["source_url"],
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
            payload = {"ok": False, "error": str(exc), "source_url": JOIN_LEAVE_URL}
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
