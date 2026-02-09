from http.server import BaseHTTPRequestHandler
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from Royale_api import get_clan_config
from war_recap_metrics import collect_recap_data, LEADERBOARD_URL_DEFAULT


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            clan_tag = params.get("clan", [""])[0]
            clan_config = get_clan_config(clan_tag)

            payload = collect_recap_data(
                clan_tag=clan_config.get("tag"),
                clan_name=clan_config.get("name"),
                leaderboard_url=clan_config.get("leaderboard_url", LEADERBOARD_URL_DEFAULT),
            )
            payload["ok"] = True
            payload["generated_at"] = datetime.now(timezone.utc).isoformat()
            payload["clan_tag"] = clan_config.get("tag")
            payload["clan_name"] = clan_config.get("name")

            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            payload = {"ok": False, "error": str(exc)}
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
