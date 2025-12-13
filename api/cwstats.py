from http.server import BaseHTTPRequestHandler
import json
from datetime import datetime, timezone

from cwstats_race import (
    fetch_soup,
    parse_race_rows,
    parse_clan_stats,
    parse_battles_left_today,
    format_race_rows,
    format_clan_stats,
    format_battles_left_today,
)

DEFAULT_URL = "https://cwstats.com/clan/9YP8UY/race"

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            soup = fetch_soup(DEFAULT_URL)

            rows = parse_race_rows(soup)
            stats = parse_clan_stats(soup)
            buckets = parse_battles_left_today(soup)

            race_text = format_race_rows(rows) if rows else ""
            clan_stats_text = format_clan_stats(stats) if stats else ""
            battles_left_text = format_battles_left_today(buckets) if buckets else ""

            payload = {
                "ok": True,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "race_text": race_text,
                "clan_stats_text": clan_stats_text,
                "battles_left_text": battles_left_text,
            }

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
