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
    render_player_table,
    render_clan_overview_table,
    render_clan_insights,
    render_clan_stats_block,
    render_clan_avg_projection,
    render_battles_left_today,
    render_risk_left_attacks,
    render_high_fame_players,
    collect_day1_high_famers,
    render_day1_high_fame_players,
    render_day4_last_chance_players,
    build_short_story,
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

            race_overview_text = render_clan_overview_table(clans)
            insights_text = render_clan_insights(clans, OUR_CLAN_NAME_DEFAULT)
            clan_stats_text = render_clan_stats_block(
                race_soup, clans, OUR_CLAN_NAME_DEFAULT, filtered_players
            )
            clan_avg_projection_text = render_clan_avg_projection(clans)
            players_text = render_player_table(filtered_players)
            battles_left_text = render_battles_left_today(filtered_players)
            risk_left_text = render_risk_left_attacks(filtered_players)
            high_fame_text = render_high_fame_players(race_soup, filtered_players)
            day1_high_famers = collect_day1_high_famers(race_soup, filtered_players)
            day1_high_fame_text = render_day1_high_fame_players(
                race_soup, filtered_players
            )
            day4_last_chance_text = render_day4_last_chance_players(
                race_soup, filtered_players
            )
            short_story_limit = 220
            short_story_text = build_short_story(
                race_soup,
                clans,
                OUR_CLAN_NAME_DEFAULT,
                filtered_players,
                max_chars=short_story_limit,
            )

            sections = [
                ("Race overview", race_overview_text),
                ("Insights", insights_text),
                ("Clan stats", clan_stats_text),
                ("Clan averages", clan_avg_projection_text),
                ("Players", players_text),
                ("Battles left", battles_left_text),
                ("Risk left", risk_left_text),
                ("High fame", high_fame_text),
                ("Day 1 high fame", day1_high_fame_text),
                ("Day 4 last chance", day4_last_chance_text),
                ("Short story", short_story_text),
            ]
            copy_all_parts = []
            for title, text in sections:
                if not text:
                    continue
                copy_all_parts.append(title)
                copy_all_parts.append(text)
            copy_all_text = "\n\n".join(copy_all_parts)

            payload = {
                "ok": True,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "race_overview_text": race_overview_text,
                "insights_text": insights_text,
                "clan_stats_text": clan_stats_text,
                "clan_avg_projection_text": clan_avg_projection_text,
                "players_text": players_text,
                "battles_left_text": battles_left_text,
                "risk_left_text": risk_left_text,
                "high_fame_text": high_fame_text,
                "day1_high_famers": [
                    {"name": name, "fame": fame}
                    for name, fame in day1_high_famers
                ],
                "day1_high_fame_text": day1_high_fame_text,
                "day4_last_chance_text": day4_last_chance_text,
                "short_story_text": short_story_text,
                "short_story_limit": short_story_limit,
                "copy_all_text": copy_all_text,
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
