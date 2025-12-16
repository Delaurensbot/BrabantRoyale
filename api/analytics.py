from http.server import BaseHTTPRequestHandler
import json
from datetime import datetime, timezone

from war_analytics_metrics import collect_analytics_data, ANALYTICS_URL_DEFAULT, CLAN_MEMBERS_URL_DEFAULT


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            payload = collect_analytics_data(
                analytics_url=ANALYTICS_URL_DEFAULT,
                members_url=CLAN_MEMBERS_URL_DEFAULT,
                top_n=10,
            )
            payload["ok"] = True
            payload["generated_at"] = datetime.now(timezone.utc).isoformat()

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
