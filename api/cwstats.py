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
    compute_total_players_participated,
    bucket_open_players,
    clean_text,
    dedupe_rows,
    extract_player_tag_from_href,
    fetch_html,
    get_clan_config,
    fetch_clan_members,
    parse_day_number,
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


def parse_clan_access_type_from_html(html: str):
    soup = BeautifulSoup(html or "", "html.parser")
    for value_el in soup.select("div.value"):
        value_text = value_el.get_text(" ", strip=True)
        if not value_text:
            continue

        normalized = value_text.lower()
        if normalized == "invite only":
            return "Invite Only"
        if normalized == "open":
            return "Open"

    return None


LAST_SEEN_PATTERN = re.compile(
    r"^(?:\d+d(?:\s+\d+h)?(?:\s+\d+m)?|\d+h(?:\s+\d+m)?|\d+m)$",
    flags=re.IGNORECASE,
)


def parse_last_seen_by_player_from_html(html: str):
    soup = BeautifulSoup(html or "", "html.parser")
    by_tag = {}
    by_name = {}

    for tr in soup.select("tr"):
        player_link = tr.select_one('a[href*="/player/"]')
        if not player_link:
            continue

        tag = extract_player_tag_from_href(player_link.get("href", "") or "")
        name = clean_text(player_link.get_text(" ", strip=True))
        cells = [clean_text(td.get_text(" ", strip=True)) for td in tr.find_all("td")]

        last_seen = next((cell for cell in cells if LAST_SEEN_PATTERN.fullmatch(cell or "")), None)
        if not last_seen:
            continue

        if tag:
            by_tag[tag] = last_seen
        if name:
            by_name[name] = last_seen

    return by_tag, by_name


def build_battles_left_groups(rows, last_seen_by_tag, last_seen_by_name):
    buckets = bucket_open_players(rows)
    groups = []

    for attacks_left in [4, 3, 2, 1]:
        names = buckets.get(attacks_left, [])
        if not names:
            continue

        players = []
        for row in rows:
            name = (row.get("name") or "").strip()
            if not name or name not in names:
                continue
            tag = (row.get("tag") or "").strip().upper()
            last_seen = last_seen_by_tag.get(tag) or last_seen_by_name.get(name) or "-"
            players.append({"name": name, "last_seen": last_seen})

        groups.append({"attacks_left": attacks_left, "players": players})

    return groups

def pick_clan_config(path: str):
    parsed = urlparse(path)
    params = parse_qs(parsed.query)
    return get_clan_config(params.get("clan", [""])[0])


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            clan_config = pick_clan_config(self.path)
            clan_html = fetch_html(clan_config["clan_url"])
            clan_tags, clan_names = fetch_clan_members(clan_config["clan_url"])
            clan_access_type = parse_clan_access_type_from_html(clan_html)
            last_seen_by_tag, last_seen_by_name = parse_last_seen_by_player_from_html(clan_html)

            race_html = fetch_html(clan_config["race_url"])
            race_soup = BeautifulSoup(race_html, "html.parser")
            day_num = parse_day_number(race_soup)
            cw_official_started = day_num in {1, 2, 3, 4}

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
            total_players_participated = compute_total_players_participated(filtered_players)

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
            battles_left_groups = build_battles_left_groups(
                filtered_players,
                last_seen_by_tag,
                last_seen_by_name,
            )
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
                "battles_left_groups": battles_left_groups,
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
                "total_players_participated": total_players_participated,
                "clan_access_type": clan_access_type,
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
