from http.server import BaseHTTPRequestHandler
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse
import re

from bs4 import BeautifulSoup

from Royale_api import (
    OUR_CLAN_NAME_DEFAULT,
    RACE_URL_DEFAULT,
    CLAN_URL_DEFAULT,
    build_short_story,
    collect_day1_high_famers,
    dedupe_rows,
    fetch_clan_members,
    fetch_html,
    get_clan_config,
    parse_clan_overview_from_race_soup,
    parse_player_rows_from_race_soup,
    render_battles_left_today,
    render_clan_avg_projection,
    render_clan_insights,
    render_clan_overview_table,
    render_clan_stats_block,
    render_day1_high_fame_players,
    render_day4_last_chance_players,
    render_high_fame_players,
    render_player_table,
    render_risk_left_attacks,
)



def _compact_number(raw: str):
    digits = re.sub(r"[^0-9]", "", (raw or ""))
    return int(digits) if digits else None


def parse_cwstats_finish_outlook_from_html(html: str):
    soup = BeautifulSoup(html or "", "html.parser")
    blob = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))

    def extract_number(pattern: str):
        m = re.search(pattern, blob, flags=re.IGNORECASE)
        return _compact_number(m.group(1)) if m else None

    def extract_rank_score(pattern: str):
        m = re.search(pattern, blob, flags=re.IGNORECASE)
        if not m:
            return None, None
        rank = _compact_number(m.group(1))
        score = _compact_number(m.group(2))
        return rank, score

    projected_rank, projected_finish = extract_rank_score(r"(\d+(?:st|nd|rd|th))\s*Projected\s*Finish\s*([\d.,]+)")
    best_rank, best_finish = extract_rank_score(r"(\d+(?:st|nd|rd|th))\s*Best\s*Possible\s*Finish\s*([\d.,]+)")
    worst_rank, worst_finish = extract_rank_score(r"(\d+(?:st|nd|rd|th))\s*Worst\s*Possible\s*Finish\s*([\d.,]+)")

    return {
        "battles_left": extract_number(r"Battles\s*Left\s*([\d.,]+)"),
        "duels_left": extract_number(r"Duels\s*Left\s*([\d.,]+)"),
        "projected_rank": projected_rank,
        "projected_finish": projected_finish,
        "best_rank": best_rank,
        "best_finish": best_finish,
        "worst_rank": worst_rank,
        "worst_finish": worst_finish,
    }


def parse_clan_join_policy_from_html(html: str):
    soup = BeautifulSoup(html or "", "html.parser")

    # Preferred layout: label/value pairs where label is "Type" and value is "Invite Only" / "Open".
    for row in soup.select("div.item"):
        label = row.select_one(".label")
        value = row.select_one(".value")
        if not label or not value:
            continue
        if (label.get_text(" ", strip=True) or "").lower() == "type":
            return value.get_text(" ", strip=True)

    text_blob = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    if re.search(r"\bInvite\s+Only\b", text_blob, flags=re.IGNORECASE):
        return "Invite Only"
    if re.search(r"\bOpen\b", text_blob, flags=re.IGNORECASE):
        return "Open"
    return None


def is_cw_official_started(race_soup: BeautifulSoup):
    page_text = race_soup.get_text(" ", strip=True)
    return bool(re.search(r"\bDay\s+\d+\b", page_text, flags=re.IGNORECASE))

def pick_clan_config(path: str):
    parsed = urlparse(path)
    params = parse_qs(parsed.query)
    return get_clan_config(params.get("clan", [""])[0])


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            clan_config = pick_clan_config(self.path)
            clan_html = fetch_html(clan_config["clan_url"])
            clan_join_policy = parse_clan_join_policy_from_html(clan_html)
            clan_tags, clan_names = fetch_clan_members(clan_config["clan_url"])

            race_html = fetch_html(clan_config["race_url"])
            race_soup = BeautifulSoup(race_html, "html.parser")
            cw_official_started = is_cw_official_started(race_soup)

            cwstats_race_url = f"https://cwstats.com/clan/{clan_config.get('tag')}/race"
            cwstats_finish_outlook = {}
            try:
                cwstats_html = fetch_html(cwstats_race_url)
                cwstats_finish_outlook = parse_cwstats_finish_outlook_from_html(cwstats_html)
            except Exception:
                cwstats_finish_outlook = {}

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
            insights_text = render_clan_insights(clans, clan_config.get("name") or OUR_CLAN_NAME_DEFAULT)
            clan_stats_text = render_clan_stats_block(
                race_soup,
                clans,
                clan_config.get("name") or OUR_CLAN_NAME_DEFAULT,
                filtered_players,
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
                clan_config.get("name") or OUR_CLAN_NAME_DEFAULT,
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
                "clan_tag": clan_config.get("tag"),
                "clan_name": clan_config.get("name"),
                "copy_all_text": copy_all_text,
                "finish_outlook": cwstats_finish_outlook,
                "clan_join_policy": clan_join_policy,
                "cw_official_started": cw_official_started,
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
