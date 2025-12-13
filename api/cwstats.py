from __future__ import annotations

import json
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler

from cwstats_race import (
    DEFAULT_URL,
    fetch_soup,
    format_battles_left_today,
    format_clan_stats,
    format_race_rows,
    parse_battles_left_today,
    parse_clan_stats,
    parse_race_rows,
)


class handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 (vercel handler signature)
        try:
            soup = fetch_soup(DEFAULT_URL)

            race_text = format_race_rows(parse_race_rows(soup))
            clan_stats_text = format_clan_stats(parse_clan_stats(soup))
            battles_left_text = format_battles_left_today(parse_battles_left_today(soup))

            generated_at = datetime.now(timezone.utc).isoformat()

            self._send_json(
                200,
                {
                    "ok": True,
                    "generated_at": generated_at,
                    "race_text": race_text or "",
                    "clan_stats_text": clan_stats_text or "",
                    "battles_left_text": battles_left_text or "",
                },
            )
        except Exception as exc:  # pragma: no cover - simple error passthrough
            self._send_json(500, {"ok": False, "error": str(exc)})
