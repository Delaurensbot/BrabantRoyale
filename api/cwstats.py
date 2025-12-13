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


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


class handler(BaseHTTPRequestHandler):  # noqa: N801 - vercel requires lowercase name
    def do_GET(self):
        try:
            soup = fetch_soup(DEFAULT_URL)

            race_text = format_race_rows(parse_race_rows(soup))
            clan_stats_text = format_clan_stats(parse_clan_stats(soup))
            battles_left_text = format_battles_left_today(
                parse_battles_left_today(soup)
            )

            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

            _json_response(
                self,
                200,
                {
                    "ok": True,
                    "generated_at": now,
                    "race_text": race_text,
                    "clan_stats_text": clan_stats_text,
                    "battles_left_text": battles_left_text,
                },
            )
        except Exception as exc:  # pragma: no cover - simple error reporting
            _json_response(
                self,
                500,
                {
                    "ok": False,
                    "error": str(exc),
                },
            )

