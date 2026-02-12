from http.server import BaseHTTPRequestHandler
import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

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


def parse_cwstats_overview(html: str):
    soup = BeautifulSoup(html, "html.parser")
    tokens = [t.strip() for t in soup.stripped_strings if t and t.strip()]
    rows = []
    i = 0

    def parse_number(raw: str):
        txt = raw.replace(",", ".")
        if re.fullmatch(r"\d+", txt):
            return int(txt)
        if re.fullmatch(r"\d+\.\d+", txt):
            return float(txt)
        return None

    while i < len(tokens):
        if not re.fullmatch(r"\d+", tokens[i]):
            i += 1
            continue

        rank = int(tokens[i])
        j = i + 1
        if j < len(tokens) and tokens[j].lower() == "badge":
            j += 1

        name = tokens[j] if j < len(tokens) else ""
        if not name:
            i += 1
            continue
        j += 1

        trophy = boat = fame = None
        if j < len(tokens) and re.fullmatch(r"\d+", tokens[j]):
            trophy = int(tokens[j])
            j += 1

        while j + 1 < len(tokens):
            label = tokens[j].lower()
            value = parse_number(tokens[j + 1])

            if label == "cw trophy" and isinstance(value, int):
                j += 2
                continue
            if label == "boat movement" and isinstance(value, int):
                boat = value
                j += 2
                continue
            if label == "fame" and isinstance(value, (int, float)):
                fame = float(value)
                j += 2
                break

            if re.fullmatch(r"\d+", tokens[j]):
                break
            j += 1

        if trophy is not None or boat is not None or fame is not None:
            rows.append(
                {
                    "rank": rank,
                    "name": name,
                    "trophies": trophy,
                    "boat": boat,
                    "fame_avg": fame,
                }
            )
            i = j
        else:
            i += 1

    deduped = []
    seen = set()
    for row in rows:
        key = (row["rank"], row["name"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)

    deduped.sort(key=lambda r: r["rank"])
    return deduped


def render_cwstats_overview_table(rows):
    if not rows:
        return ""

    rank_w = max([len(str(r["rank"])) for r in rows] + [len("#")])
    name_w = max([len(r["name"]) for r in rows] + [len("Clan")])
    trophy_w = max([len(str(r.get("trophies") or "")) for r in rows] + [len("Trophies")])
    boat_w = max([len(str(r.get("boat") or "")) for r in rows] + [len("Boat")])
    fame_w = max([
        len(f"{r.get('fame_avg'):.2f}") if r.get("fame_avg") is not None else 0
        for r in rows
    ] + [len("Fame")])

    header = (
        f"{'#':>{rank_w}} | {'Clan':<{name_w}} | {'Trophies':>{trophy_w}} | "
        f"{'Boat':>{boat_w}} | {'Fame':>{fame_w}}"
    )
    out = ["Clan overview (cwstats):", header, "-" * len(header)]

    for r in rows:
        fame_txt = "" if r.get("fame_avg") is None else f"{r['fame_avg']:.2f}"
        out.append(
            f"{r['rank']:>{rank_w}} | {r['name']:<{name_w}} | "
            f"{str(r.get('trophies') or ''):>{trophy_w}} | "
            f"{str(r.get('boat') or ''):>{boat_w}} | {fame_txt:>{fame_w}}"
        )

    return "\n".join(out)

def pick_clan_config(path: str):
    parsed = urlparse(path)
    params = parse_qs(parsed.query)
    return get_clan_config(params.get("clan", [""])[0])


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            clan_config = pick_clan_config(self.path)
            clan_tags, clan_names = fetch_clan_members(clan_config["clan_url"])

            race_html = fetch_html(clan_config["race_url"])
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
            if "niet gevonden" in race_overview_text.lower():
                cwstats_html = fetch_html(clan_config["cwstats_race_url"])
                cwstats_rows = parse_cwstats_overview(cwstats_html)
                cwstats_overview = render_cwstats_overview_table(cwstats_rows)
                if cwstats_overview:
                    race_overview_text = cwstats_overview
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
