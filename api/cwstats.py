from http.server import BaseHTTPRequestHandler
import json
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from Royale_api import (
    RACE_URL_DEFAULT,
    CLAN_URL_DEFAULT,
    OUR_CLAN_NAME_DEFAULT,
    fetch_html,
    fetch_clan_members,
    parse_clan_overview_from_race_soup,
    parse_player_rows_from_race_soup,
    dedupe_rows,
    render_clan_overview_table,
    render_clan_insights,
    render_clan_stats_block,
    render_battles_left_today,
)

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            clan_tags, clan_names = fetch_clan_members(CLAN_URL_DEFAULT)

            race_html = fetch_html(RACE_URL_DEFAULT)
            race_soup = BeautifulSoup(race_html, "html.parser")

            clans = parse_clan_overview_from_race_soup(race_soup)
            players = parse_player_rows_from_race_soup(race_soup)

            filtered_players = []
            for row in players:
                tag = (row.get("tag") or "").strip().upper()
                name = (row.get("name") or "").strip()
                if (tag and tag in clan_tags) or (name and name in clan_names):
                    filtered_players.append(row)

            filtered_players = sorted(filtered_players, key=lambda r: int(r.get("rank", 0) or 0))
            filtered_players = dedupe_rows(filtered_players)

            race_text_parts = []
            overview = render_clan_overview_table(clans)
            if overview:
                race_text_parts.append(overview)

            insights = render_clan_insights(clans, OUR_CLAN_NAME_DEFAULT)
            if insights:
                race_text_parts.append(insights)

            race_text = "\n\n".join(part for part in race_text_parts if part)

            clan_stats_text = render_clan_stats_block(
                race_soup, clans, OUR_CLAN_NAME_DEFAULT, filtered_players
            )

            battles_left_text = render_battles_left_today(filtered_players)

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
