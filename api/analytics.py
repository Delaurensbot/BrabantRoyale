from http.server import BaseHTTPRequestHandler
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from Royale_api import get_clan_config
from war_analytics_metrics import collect_analytics_data, ANALYTICS_URL_DEFAULT, CLAN_MEMBERS_URL_DEFAULT


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            clan_tag = params.get("clan", [""])[0]
            clan_config = get_clan_config(clan_tag)
            payload = collect_analytics_data(
                analytics_url=clan_config.get("analytics_url", ANALYTICS_URL_DEFAULT),
                members_url=clan_config.get("clan_url", CLAN_MEMBERS_URL_DEFAULT),
                war_log_url=clan_config.get("war_log_url"),
                war_ranking_url=clan_config.get("war_ranking_url"),
                clan_tag=clan_config.get("tag"),
                top_n=10,
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

        except Exception as e:
            payload = {"ok": False, "error": str(e)}
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
