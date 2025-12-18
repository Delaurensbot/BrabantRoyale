#!/usr/bin/env python3
# royaleapi_war_analytics_reliability_v4.py
#
# Changes:
# - MVP previous season: only Player + Score, season only in title, only eligible players included.
# - Current season leaderboard: only Player + Score, only "perfect" players (no missed attacks on played weekends).
# - Seasons are detected dynamically: current = max season in headers, previous = second max.

import argparse
import math
import re
import sys
from typing import List, Optional, Tuple, Dict, Set
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

from Royale_api import DEFAULT_CLAN_TAG, get_clan_config


DEFAULT_CLAN_CONFIG = get_clan_config(DEFAULT_CLAN_TAG)
ANALYTICS_URL_DEFAULT = DEFAULT_CLAN_CONFIG["analytics_url"]
CLAN_MEMBERS_URL_DEFAULT = DEFAULT_CLAN_CONFIG["clan_url"]

KNOWN_ROLES = ["Leader", "Co-leader", "Elder", "Member"]
ROLE_DISPLAY = {"Leader": "Owner"}  # RoyaleAPI gebruikt vaak "Leader"; jij wil "Owner"

UNREPLACEABLE_PENALTY = {0: 0, 1: 2, 2: 4, 3: 12}


def normalize_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def clean_player_name(s: str) -> str:
    s = normalize_space(s)
    s = re.sub(r"<[^>]+>", "", s)
    return normalize_space(s).lower()


def is_number_like(s: str) -> bool:
    s = normalize_space(s)
    if s == "" or "/" in s:
        return False
    s2 = s.replace(",", "")
    return bool(re.fullmatch(r"-?\d+(\.\d+)?", s2))


def format_table(title: str, headers: List[str], rows: List[List[str]], limit: Optional[int] = None) -> str:
    if limit is not None and limit > 0:
        rows = rows[:limit]

    headers = [normalize_space(h) for h in headers]
    rows = [[normalize_space(c) for c in r] for r in rows]

    width = len(headers)
    fixed_rows = []
    for r in rows:
        if len(r) < width:
            r = r + [""] * (width - len(r))
        elif len(r) > width:
            r = r[:width]
        fixed_rows.append(r)
    rows = fixed_rows

    col_widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            col_widths[i] = max(col_widths[i], len(c))

    right_align = []
    for i in range(width):
        numeric_count = 0
        nonempty = 0
        for r in rows:
            if r[i] != "":
                nonempty += 1
                if is_number_like(r[i]):
                    numeric_count += 1
        right_align.append(nonempty > 0 and numeric_count / nonempty >= 0.7)

    def render_row(vals: List[str]) -> str:
        out = []
        for i, v in enumerate(vals):
            if right_align[i]:
                out.append(v.rjust(col_widths[i]))
            else:
                out.append(v.ljust(col_widths[i]))
        return " | ".join(out)

    sep = "-+-".join("-" * w for w in col_widths)

    lines = []
    lines.append(f"\n{title}")
    lines.append(render_row(headers))
    lines.append(sep)
    for r in rows:
        lines.append(render_row(r))
    return "\n".join(lines)


def fetch(url: str, timeout: int = 25) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,nl;q=0.8",
        "Connection": "close",
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} while fetching {url}")
    return r.text


def extract_player_tag_from_href(href: str) -> Optional[str]:
    if not href:
        return None
    href_decoded = unquote(href)
    href_u = href_decoded.upper()
    m = re.search(r"/PLAYER/(?:#)?([A-Z0-9]+)", href_u)
    return m.group(1) if m else None


def extract_role_from_row_text(row_text: str) -> str:
    t = normalize_space(row_text)
    for role in KNOWN_ROLES:
        if re.search(rf"\b{re.escape(role)}\b", t, flags=re.IGNORECASE):
            return role
    return ""


def get_current_members_with_roles(members_url: str) -> Tuple[Dict[str, str], Dict[str, str], Dict[str, str]]:
    html = fetch(members_url)
    soup = BeautifulSoup(html, "html.parser")

    tag_to_name_clean: Dict[str, str] = {}
    name_clean_to_tag: Dict[str, str] = {}
    tag_to_role: Dict[str, str] = {}

    for tr in soup.find_all("tr"):
        a = tr.find("a", href=True)
        if not a:
            continue

        tag = extract_player_tag_from_href(a["href"])
        if not tag:
            continue

        name_raw = a.get_text(" ", strip=True)
        name_clean = clean_player_name(name_raw)
        if not name_clean:
            continue

        role = extract_role_from_row_text(tr.get_text(" ", strip=True))
        role = ROLE_DISPLAY.get(role, role)

        tag_to_name_clean[tag] = name_clean
        name_clean_to_tag[name_clean] = tag
        if role:
            tag_to_role[tag] = role

    return tag_to_name_clean, name_clean_to_tag, tag_to_role


def get_table_headers(table: BeautifulSoup) -> List[str]:
    thead = table.find("thead")
    if thead:
        return [normalize_space(th.get_text(" ", strip=True)) for th in thead.find_all("th")]
    first_row = table.find("tr")
    if first_row:
        return [normalize_space(x.get_text(" ", strip=True)) for x in first_row.find_all(["th", "td"])]
    return []


def find_table_by_headers(soup: BeautifulSoup, must_have: Set[str]) -> Optional[BeautifulSoup]:
    must_have_lower = {h.lower() for h in must_have}
    for table in soup.find_all("table"):
        headers = get_table_headers(table)
        hset = {h.lower() for h in headers}
        if must_have_lower.issubset(hset):
            return table
    return None


def parse_table_with_tag_or_name(table: BeautifulSoup) -> Tuple[List[str], List[List[str]], List[Optional[str]], List[str]]:
    headers = get_table_headers(table)

    tbody = table.find("tbody")
    row_tags = tbody.find_all("tr") if tbody else table.find_all("tr")[1:]

    rows: List[List[str]] = []
    tags_per_row: List[Optional[str]] = []
    names_per_row: List[str] = []

    for tr in row_tags:
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue

        player_cell = cells[0]
        row_tag = None
        a = player_cell.find("a", href=True)
        if a:
            row_tag = extract_player_tag_from_href(a["href"])

        row = [normalize_space(c.get_text(" ", strip=True)) for c in cells]
        if not row or not any(x != "" for x in row):
            continue

        player_name_clean = clean_player_name(row[0])
        row[0] = re.sub(r"<[^>]+>", "", row[0]).strip()

        rows.append(row)
        tags_per_row.append(row_tag)
        names_per_row.append(player_name_clean)

    return headers, rows, tags_per_row, names_per_row


def compute_mvp_list(
    season_weeks: List[str],
    contrib_map: Dict[str, Dict[str, int]],
    decks_map: Dict[str, Dict[str, int]],
    player_print_map: Dict[str, str],
    top_n: int,
    require_all_weekends: bool,
) -> List[Dict[str, str]]:
    results: List[Dict[str, str]] = []

    for key, per_week_c in contrib_map.items():
        total_score = 0
        eligible = True

        for wh in season_weeks:
            c_val = per_week_c.get(wh, 0)

            # Alleen meegerekend als Contribution > 0
            if c_val <= 0:
                if require_all_weekends:
                    eligible = False
                continue

            d_val = decks_map.get(key, {}).get(wh, 0)
            if d_val != 16:
                eligible = False
                break

            total_score += c_val

        if eligible and total_score > 0:
            results.append({
                "player": player_print_map.get(key, key),
                "score": str(total_score),
            })

    results.sort(key=lambda r: int(r.get("score", 0)), reverse=True)
    return results[:top_n]


def filter_rows_keep_alignment(rows, tags, names, current_tags, name_to_tag):
    f_rows, f_tags, f_names = [], [], []
    for row, tag, nm in zip(rows, tags, names):
        name_is_current = nm in name_to_tag
        tag_is_current = (tag is not None) and (tag in current_tags)
        if tag_is_current or name_is_current:
            f_rows.append(row)
            f_tags.append(tag)
            f_names.append(nm)
    return f_rows, f_tags, f_names


def add_role_column(headers: List[str], rows: List[List[str]], tags: List[Optional[str]], names: List[str],
                    name_to_tag: Dict[str, str], tag_to_role: Dict[str, str]) -> Tuple[List[str], List[List[str]]]:
    new_headers = headers[:]
    if new_headers and new_headers[0].lower() == "player":
        new_headers.insert(1, "Role")
    else:
        new_headers = ["Player", "Role"] + new_headers[1:]

    new_rows: List[List[str]] = []
    for row, tag, nm in zip(rows, tags, names):
        use_tag = tag or name_to_tag.get(nm)
        role = tag_to_role.get(use_tag, "") if use_tag else ""
        new_row = row[:]
        new_row.insert(1, role)
        new_rows.append(new_row)

    return new_headers, new_rows


def parse_int_cell(cell: str) -> Optional[int]:
    c = normalize_space(cell)
    if c == "":
        return None
    if not re.fullmatch(r"-?\d+", c):
        return None
    return int(c)


def season_of_week_header(wh: str) -> Optional[int]:
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", wh)
    if not m:
        return None
    return int(m.group(1))


def build_maps(contrib_headers2, contrib_rows2, decks_headers2, decks_rows2):
    contrib_headers_lower = [h.lower() for h in contrib_headers2]
    decks_headers_lower = [h.lower() for h in decks_headers2]

    c_idx = contrib_headers_lower.index("c")
    d_idx = decks_headers_lower.index("d")

    contrib_week_headers = contrib_headers2[c_idx + 1:]
    decks_week_headers = decks_headers2[d_idx + 1:]

    contrib_map: Dict[str, Dict[str, int]] = {}
    decks_map: Dict[str, Dict[str, int]] = {}
    role_map: Dict[str, str] = {}
    player_print_map: Dict[str, str] = {}

    for r in contrib_rows2:
        player_print = r[0]
        key = clean_player_name(player_print)
        role = r[1] if len(r) > 1 else ""
        role_map[key] = role
        player_print_map[key] = player_print

        week_cells = r[c_idx + 1:]
        per_week: Dict[str, int] = {}
        for wh, cell in zip(contrib_week_headers, week_cells):
            v = parse_int_cell(cell)
            if v is None:
                continue
            per_week[wh] = v
        contrib_map[key] = per_week

    for r in decks_rows2:
        player_print = r[0]
        key = clean_player_name(player_print)
        player_print_map.setdefault(key, player_print)

        week_cells = r[d_idx + 1:]
        per_week: Dict[str, int] = {}
        for wh, cell in zip(decks_week_headers, week_cells):
            v = parse_int_cell(cell)
            if v is None:
                continue
            per_week[wh] = max(0, min(16, v))
        decks_map[key] = per_week

    return contrib_week_headers, decks_week_headers, contrib_map, decks_map, role_map, player_print_map


def compute_reliability_scores(
    contrib_map: Dict[str, Dict[str, int]],
    decks_map: Dict[str, Dict[str, int]],
    role_map: Dict[str, str],
    player_print_map: Dict[str, str],
) -> List[Dict[str, object]]:
    results: List[Dict[str, object]] = []

    for key, per_week_c in contrib_map.items():
        weeks_played = 0
        missed_attacks = 0
        penalty_points = 0
        attacks_done = 0
        total_points = 0

        for wh, c_val in per_week_c.items():
            if c_val <= 0:
                continue

            weeks_played += 1
            total_points += c_val
            d_val = decks_map.get(key, {}).get(wh, 0)
            done = max(0, min(16, d_val))
            attacks_done += done
            missing = max(0, 16 - done)
            missed_attacks += missing
            penalty_points += UNREPLACEABLE_PENALTY.get(missing, missing * 4)

        total_possible = weeks_played * 16
        reliability_score = 0.0
        avg_points = 0.0
        if total_possible > 0:
            reliability_score = round((attacks_done / total_possible) * 100, 2)
            avg_points = round(total_points / weeks_played, 2)

        results.append(
            {
                "player": player_print_map.get(key, key),
                "role": role_map.get(key, ""),
                "weeks_played": weeks_played,
                "attacks_done": attacks_done,
                "missed_attacks": missed_attacks,
                "penalty_points": penalty_points,
                "avg_points": avg_points,
                "reliability_score": reliability_score,
            }
        )

    results.sort(key=lambda r: (r.get("reliability_score", 0), r.get("missed_attacks", 0)))
    return results


def parse_week_key(week_header: str) -> Tuple[int, int]:
    m = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", week_header)
    if not m:
        return (math.inf, math.inf)
    return (int(m.group(1)), int(m.group(2)))


def sort_valid_weeks(decks_by_week: Dict[str, int]) -> List[Tuple[Tuple[int, int], str, int]]:
    ordered: List[Tuple[Tuple[int, int], str, int]] = []
    for week_header, decks_used in decks_by_week.items():
        parsed = parse_week_key(week_header)
        if math.isinf(parsed[0]) or math.isinf(parsed[1]):
            continue
        ordered.append((parsed, week_header, decks_used))
    ordered.sort(key=lambda t: t[0])
    return ordered


def current_perfect_streak(decks_by_week: Dict[str, int]) -> int:
    ordered = sort_valid_weeks(decks_by_week)
    streak = 0
    for _, _, decks_used in reversed(ordered):
        if decks_used == 16:
            streak += 1
        else:
            break
    return streak


ELDER_REQUIRED_STREAK = 6
ELDER_MIN_AVG_CONTRIB = 2500


def last_n_weeks_all_perfect(decks_by_week: Dict[str, int], n: int = ELDER_REQUIRED_STREAK) -> bool:
    ordered = sort_valid_weeks(decks_by_week)
    if len(ordered) < n:
        return False
    last_n = ordered[-n:]
    return all(decks_used == 16 for _, _, decks_used in last_n)


def should_promote_to_elder(player_name: str, role: str, decks_by_week: Dict[str, int]) -> bool:
    """
    True als:
    - huidige role == "Member"
    - huidige streak >= 6 weekends met D == 16
    """

    if (role or "").strip().lower() != "member":
        return False

    streak = current_perfect_streak(decks_by_week)
    if streak < ELDER_REQUIRED_STREAK:
        return False

    return last_n_weeks_all_perfect(decks_by_week, n=ELDER_REQUIRED_STREAK)


def average_contribution(per_week_contrib: Dict[str, int]) -> float:
    played_weeks = [v for v in per_week_contrib.values() if v > 0]
    if not played_weeks:
        return 0.0
    return round(sum(played_weeks) / len(played_weeks), 2)


def build_promotion_candidates(
    contrib_map: Dict[str, Dict[str, int]],
    decks_map: Dict[str, Dict[str, int]],
    role_map: Dict[str, str],
    player_print_map: Dict[str, str],
) -> List[Dict[str, object]]:
    suggestions: List[Dict[str, object]] = []

    for key, per_week_decks in decks_map.items():
        role = role_map.get(key, "")
        if not should_promote_to_elder(key, role, per_week_decks):
            continue

        streak = current_perfect_streak(per_week_decks)
        avg_score = average_contribution(contrib_map.get(key, {}))
        if avg_score < ELDER_MIN_AVG_CONTRIB:
            continue
        suggestions.append(
            {
                "player": player_print_map.get(key, key),
                "streak_weeks": streak,
                "average_contribution": avg_score,
                "reason": (
                    f"Laatste {ELDER_REQUIRED_STREAK} weken perfecte attacks (D=16) als Member "
                    f"en Gem. C ≥ {ELDER_MIN_AVG_CONTRIB}."
                ),
            }
        )

    suggestions.sort(key=lambda r: (r.get("streak_weeks", 0), r.get("average_contribution", 0)), reverse=True)
    return suggestions


def detect_current_and_previous_season(week_headers: List[str]) -> Tuple[Optional[int], Optional[int]]:
    seasons = sorted({s for s in (season_of_week_header(wh) for wh in week_headers) if s is not None})
    if not seasons:
        return None, None
    current = seasons[-1]
    prev = seasons[-2] if len(seasons) >= 2 else None
    return current, prev


def build_previous_season_mvp_simple(contrib_week_headers, contrib_map, decks_map, player_print_map,
                                    prev_season: int, top_n: int) -> str:
    season_weeks = [wh for wh in contrib_week_headers if season_of_week_header(wh) == prev_season]

    headers = ["Player", "Score"]
    rows: List[List[str]] = []

    for key, per_week_c in contrib_map.items():
        if not season_weeks:
            continue

        # Must play every weekend in that season: Contribution > 0 for all season weeks
        # And must be perfect: D=16 for all those weeks
        total_score = 0
        eligible = True

        for wh in season_weeks:
            c_val = per_week_c.get(wh, 0)
            if c_val <= 0:
                eligible = False
                break
            d_val = decks_map.get(key, {}).get(wh, 0)
            if d_val != 16:
                eligible = False
                break
            total_score += c_val

        if eligible:
            rows.append([player_print_map.get(key, key), str(total_score)])

    rows.sort(key=lambda r: int(r[1]), reverse=True)
    rows = rows[:top_n]

    title = f"Vorige seizoen MVP (Seizoen {prev_season}) Top {top_n}"
    if not rows:
        return f"\n{title}\nGeen spelers gevonden die perfect waren (C>0 elke week en D=16 elke week)."
    return format_table(title, headers, rows, limit=None)


def build_current_leaderboard_simple(contrib_week_headers, contrib_map, decks_map, player_print_map,
                                     current_season: int, top_n: int) -> str:
    season_weeks = [wh for wh in contrib_week_headers if season_of_week_header(wh) == current_season]

    headers = ["Player", "Score"]
    rows: List[List[str]] = []

    for key, per_week_c in contrib_map.items():
        total_score = 0
        weeks_played = 0
        perfect = True

        for wh in season_weeks:
            c_val = per_week_c.get(wh, 0)

            # Alleen "gespeeld weekend" als Contribution > 0
            if c_val <= 0:
                continue

            weeks_played += 1
            d_val = decks_map.get(key, {}).get(wh, 0)

            # Perfect rule: als je speelt, dan moet je D=16 hebben
            if d_val != 16:
                perfect = False
                break

            total_score += c_val

        if weeks_played > 0 and perfect:
            rows.append([player_print_map.get(key, key), str(total_score)])

    rows.sort(key=lambda r: int(r[1]), reverse=True)
    rows = rows[:top_n]

    title = f"Huidig seizoen perfect leaderboard (Seizoen {current_season}) Top {top_n}"
    if not rows:
        return f"\n{title}\nGeen perfecte spelers gevonden (in gespeelde weekenden moet D=16 zijn)."
    return format_table(title, headers, rows, limit=None)


def print_mvp_explanations_simple(prev_season: Optional[int], current_season: Optional[int]) -> None:
    lines = []
    lines.append("\nUitleg: Vorige seizoen MVP")
    if prev_season is None:
        lines.append("- Vorige seizoen: niet gevonden in de headers.")
    else:
        lines.append(f"- Vorige seizoen: seizoen {prev_season}")
        lines.append("- Alleen spelers die elk weekend gespeeld hebben (Contribution > 0) en perfect waren (D=16) komen erin.")
        lines.append("- Ranking: hoogste totale Contribution-score binnen seizoen.")
    lines.append("")
    lines.append("Uitleg: Huidig seizoen perfect leaderboard")
    if current_season is None:
        lines.append("- Huidig seizoen: niet gevonden in de headers.")
    else:
        lines.append(f"- Huidig seizoen: seizoen {current_season}")
        lines.append("- Alleen spelers die in hun gespeelde weekenden geen aanval misten (D=16) komen erin.")
        lines.append("- Ranking: hoogste totale Contribution-score tot nu toe binnen seizoen.")
        lines.append("- Hall of Fame pas na seizoen, maar dit is de live top 10 met jouw perfecte-regel.")
    print("\n".join(lines))


def collect_analytics_data(
    analytics_url: str = ANALYTICS_URL_DEFAULT,
    members_url: str = CLAN_MEMBERS_URL_DEFAULT,
    top_n: int = 10,
) -> Dict[str, object]:
    tag_to_name_clean, name_clean_to_tag, tag_to_role = get_current_members_with_roles(members_url)
    current_tags = set(tag_to_name_clean.keys())
    if not current_tags:
        raise RuntimeError("Could not extract current members from the clan page.")

    html = fetch(analytics_url)
    soup = BeautifulSoup(html, "html.parser")

    contribution_table = find_table_by_headers(soup, must_have={"Player", "M", "P", "C"})
    decks_table = find_table_by_headers(soup, must_have={"Player", "M", "P", "D"})

    if not contribution_table or not decks_table:
        raise RuntimeError("Required tables not found on analytics page.")

    headers_c, rows_c, tags_c, names_c = parse_table_with_tag_or_name(contribution_table)
    f_rows_c, f_tags_c, f_names_c = filter_rows_keep_alignment(
        rows_c, tags_c, names_c, current_tags, name_clean_to_tag
    )
    contrib_headers, contrib_rows = add_role_column(
        headers_c, f_rows_c, f_tags_c, f_names_c, name_clean_to_tag, tag_to_role
    )

    headers_d, rows_d, tags_d, names_d = parse_table_with_tag_or_name(decks_table)
    f_rows_d, f_tags_d, f_names_d = filter_rows_keep_alignment(
        rows_d, tags_d, names_d, current_tags, name_clean_to_tag
    )
    decks_headers, decks_rows = add_role_column(
        headers_d, f_rows_d, f_tags_d, f_names_d, name_clean_to_tag, tag_to_role
    )

    contrib_week_headers, _, contrib_map, decks_map, role_map, player_print_map = build_maps(
        contrib_headers, contrib_rows, decks_headers, decks_rows
    )

    current_season, prev_season = detect_current_and_previous_season(contrib_week_headers)

    mvp_current: List[Dict[str, str]] = []
    if current_season is not None:
        weeks_current = [wh for wh in contrib_week_headers if season_of_week_header(wh) == current_season]
        mvp_current = compute_mvp_list(
            weeks_current, contrib_map, decks_map, player_print_map, top_n, require_all_weekends=False
        )

    mvp_previous: List[Dict[str, str]] = []
    if prev_season is not None:
        weeks_prev = [wh for wh in contrib_week_headers if season_of_week_header(wh) == prev_season]
        mvp_previous = compute_mvp_list(
            weeks_prev, contrib_map, decks_map, player_print_map, top_n, require_all_weekends=True
        )

    ratio_scores = compute_reliability_scores(contrib_map, decks_map, role_map, player_print_map)
    promotion_candidates = build_promotion_candidates(contrib_map, decks_map, role_map, player_print_map)

    return {
        "mvp_current": mvp_current,
        "mvp_previous": mvp_previous,
        "ratio_scores": ratio_scores,
        "promotion_candidates": promotion_candidates,
        "contribution_table": {"headers": contrib_headers, "rows": contrib_rows},
        "decks_used_table": {"headers": decks_headers, "rows": decks_rows},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--analytics-url", default=ANALYTICS_URL_DEFAULT)
    ap.add_argument("--members-url", default=CLAN_MEMBERS_URL_DEFAULT)
    ap.add_argument("--limit", type=int, default=0, help="Limit printed rows per table (0 = no limit)")
    ap.add_argument("--top", type=int, default=10, help="Top N for MVP/leaderboards")
    args = ap.parse_args()

    try:
        tag_to_name_clean, name_clean_to_tag, tag_to_role = get_current_members_with_roles(args.members_url)
    except Exception as e:
        print(f"Failed to fetch current members: {e}", file=sys.stderr)
        return 1

    current_tags = set(tag_to_name_clean.keys())
    if not current_tags:
        print("Could not extract current members from the clan page.", file=sys.stderr)
        return 1

    try:
        html = fetch(args.analytics_url)
    except Exception as e:
        print(f"Failed to fetch analytics page: {e}", file=sys.stderr)
        return 1

    soup = BeautifulSoup(html, "html.parser")
    contribution_table = find_table_by_headers(soup, must_have={"Player", "M", "P", "C"})
    decks_table = find_table_by_headers(soup, must_have={"Player", "M", "P", "D"})

    limit = args.limit if args.limit > 0 else None
    print(f"\nCurrent members detected: {len(current_tags)}")

    contrib_headers2: Optional[List[str]] = None
    contrib_rows2: Optional[List[List[str]]] = None

    if contribution_table:
        headers, rows, tags_per_row, names_per_row = parse_table_with_tag_or_name(contribution_table)
        f_rows, f_tags, f_names = filter_rows_keep_alignment(rows, tags_per_row, names_per_row, current_tags, name_clean_to_tag)
        headers2, rows2 = add_role_column(headers, f_rows, f_tags, f_names, name_clean_to_tag, tag_to_role)
        contrib_headers2, contrib_rows2 = headers2, rows2

        print(f"\nContribution rows before filter: {len(rows)} | after filter: {len(f_rows)}")
        print(format_table("Contribution (current members only)", headers2, rows2, limit=limit))
    else:
        print("\nContribution table not found.", file=sys.stderr)

    decks_headers2: Optional[List[str]] = None
    decks_rows2: Optional[List[List[str]]] = None

    if decks_table:
        headers, rows, tags_per_row, names_per_row = parse_table_with_tag_or_name(decks_table)
        f_rows, f_tags, f_names = filter_rows_keep_alignment(rows, tags_per_row, names_per_row, current_tags, name_clean_to_tag)
        headers2, rows2 = add_role_column(headers, f_rows, f_tags, f_names, name_clean_to_tag, tag_to_role)
        decks_headers2, decks_rows2 = headers2, rows2

        print(f"\nDecks Used rows before filter: {len(rows)} | after filter: {len(f_rows)}")
        print(format_table("Decks Used (current members only)", headers2, rows2, limit=limit))
    else:
        print("\nDecks Used table not found.", file=sys.stderr)

    if contrib_headers2 and contrib_rows2 and decks_headers2 and decks_rows2:
        contrib_week_headers, _, contrib_map, decks_map, _, player_print_map = build_maps(
            contrib_headers2, contrib_rows2, decks_headers2, decks_rows2
        )

        current_season, prev_season = detect_current_and_previous_season(contrib_week_headers)

        if prev_season is not None:
            print(build_previous_season_mvp_simple(
                contrib_week_headers, contrib_map, decks_map, player_print_map, prev_season, args.top
            ))
        else:
            print("\nVorige seizoen MVP\nNiet genoeg season-data gevonden om een vorig seizoen te bepalen.")

        if current_season is not None:
            print(build_current_leaderboard_simple(
                contrib_week_headers, contrib_map, decks_map, player_print_map, current_season, args.top
            ))
        else:
            print("\nHuidig seizoen leaderboard\nGeen season-data gevonden.")

        print_mvp_explanations_simple(prev_season, current_season)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
