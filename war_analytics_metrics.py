#!/usr/bin/env python3
# royaleapi_war_analytics_reliability_v4.py
#
# Changes:
# - MVP previous season: only Player + Score, season only in title, only eligible players included.
# - Current season leaderboard: only Player + Score, only "perfect" players (no missed attacks on played weekends).
# - Seasons are detected dynamically: current = max season in headers, previous = second max.

import argparse
import os
import re
import sys
from typing import List, Optional, Tuple, Dict, Set, Mapping
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

from Royale_api import DEFAULT_CLAN_TAG, get_clan_config
from supabase_history import (
    clash_date_to_iso,
    clash_date_to_datetime,
    extract_clan_participants,
    load_history_races_from_env,
    load_week_exclusions_from_env,
    normalize_tag,
)

try:
    from api.strategy_engine import (
        build_strategic_week_metadata as _build_strategic_week_metadata,
        classify_race_phase,
    )
except ImportError:  # pragma: no cover - convenient when run as a loose script.
    from strategy_engine import (  # type: ignore
        build_strategic_week_metadata as _build_strategic_week_metadata,
        classify_race_phase,
    )


DEFAULT_CLAN_CONFIG = get_clan_config(DEFAULT_CLAN_TAG)
ANALYTICS_URL_DEFAULT = DEFAULT_CLAN_CONFIG["analytics_url"]
CLAN_MEMBERS_URL_DEFAULT = DEFAULT_CLAN_CONFIG["clan_url"]

KNOWN_ROLES = ["Leader", "Co-leader", "Elder", "Member"]
ROLE_DISPLAY = {"Leader": "Owner"}  # RoyaleAPI gebruikt vaak "Leader"; jij wil "Owner"

UNREPLACEABLE_PENALTY = {0: 0, 1: 2, 2: 4, 3: 12}
ROYAL_API_BASE_URL = "https://proxy.royaleapi.dev/v1"
PROMOTION_WINDOWS = (2, 4, 6)
DEFAULT_PROMOTION_WINDOW = 6
DEMOTION_WINDOW = 10
DEMOTION_MAX_MISSED_ATTACKS = 2

ExcludedWeeks = Set[Tuple[str, str]]


def _safe_int(value: object) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        try:
            return int(value.strip())
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def _known_int(mapping: Dict[str, object], *keys: str) -> Optional[int]:
    for key in keys:
        if key in mapping:
            return _safe_int(mapping.get(key))
    return None


def _race_value(race: Dict[str, object], *keys: str):
    for key in keys:
        if key in race and race.get(key) is not None:
            return race.get(key)
    return None


def _race_phase_metadata(race: Dict[str, object]) -> Dict[str, object]:
    """Expose official race context without using sectionIndex as a week id."""

    phase = classify_race_phase(race)
    return {
        "phase_status": phase.get("phaseStatus"),
        "phaseStatus": phase.get("phaseStatus"),
        "detected_phase_status": phase.get("detectedPhaseStatus"),
        "data_status": phase.get("dataStatus"),
        "dataStatus": phase.get("dataStatus"),
        "period_type": phase.get("periodType"),
        "periodType": phase.get("periodType"),
        "period_index": phase.get("periodIndex"),
        "periodIndex": phase.get("periodIndex"),
        "section_index": phase.get("sectionIndex"),
        "sectionIndex": phase.get("sectionIndex"),
        "finish_time": phase.get("finishTime"),
        "finishTime": phase.get("finishTime"),
        "phase_data_quality": phase.get("phaseDataQuality"),
        "phaseDataQuality": phase.get("phaseDataQuality"),
        "phase_source": phase.get("phaseSource"),
        "phaseSource": phase.get("phaseSource"),
    }


def _analytics_race_key(race: Mapping[str, object], clan_tag: str) -> Optional[str]:
    explicit = _race_value(
        dict(race),
        "raceKey",
        "race_key",
        "strategyRaceKey",
        "strategy_race_key",
    )
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()[:256]
    season_id = _safe_int(_race_value(dict(race), "seasonId", "season_id"))
    try:
        created_at = clash_date_to_iso(
            _race_value(race, "createdDate", "created_at", "race_created_at")
        )
    except (TypeError, ValueError, OverflowError):
        created_at = ""
    if season_id is None or not created_at:
        return None
    section = _safe_int(_race_value(dict(race), "sectionIndex", "section_index"))
    parts = [clan_tag, str(season_id)]
    if section is not None:
        parts.append(str(section))
    parts.append(created_at)
    return ":".join(parts)[:256]


def build_strategic_week_metadata(
    race: Optional[Mapping[str, object]] = None,
    *,
    week_key: Optional[str] = None,
    clan_tag: str = DEFAULT_CLAN_TAG,
) -> Dict[str, object]:
    """Expose the T16 strategic-week label for analytics callers."""

    source = dict(race or {})
    source.setdefault("clan_tag", clan_tag)
    metadata = _build_strategic_week_metadata(source)
    if week_key:
        metadata["week_key"] = week_key
        metadata["weekKey"] = week_key
    if metadata.get("is_strategic_week") and not metadata.get("race_key"):
        metadata["race_key"] = _analytics_race_key(source, clan_tag)
        metadata["raceKey"] = metadata["race_key"]
    return metadata


build_strategy_week_metadata = build_strategic_week_metadata


def _strategy_week_for_race(
    race: Mapping[str, object],
    week_key: str,
    clan_tag: str,
    strategy_weeks: object,
) -> Dict[str, object]:
    """Merge optional audit metadata into one race without trusting names."""

    base = dict(race)
    base["clan_tag"] = clan_tag
    race_key = _analytics_race_key(base, clan_tag)
    supplied = None
    if isinstance(strategy_weeks, Mapping):
        is_metadata = any(
            key in strategy_weeks
            for key in (
                "strategy_mode",
                "strategyMode",
                "is_strategic_week",
                "isStrategicWeek",
                "reason",
                "strategy_reason",
            )
        )
        supplied = (
            strategy_weeks
            if is_metadata
            else strategy_weeks.get(week_key)
            or (strategy_weeks.get(race_key) if race_key else None)
        )
    elif isinstance(strategy_weeks, (list, tuple)):
        for item in strategy_weeks:
            if not isinstance(item, Mapping):
                continue
            item_key = _race_value(
                dict(item),
                "week_key",
                "weekKey",
                "race_key",
                "raceKey",
            )
            if item_key in {week_key, race_key}:
                supplied = item
                break
    if isinstance(supplied, Mapping):
        base.update(supplied)
    metadata = build_strategic_week_metadata(
        base,
        week_key=week_key,
        clan_tag=clan_tag,
    )
    if metadata.get("is_strategic_week") and not metadata.get("race_key"):
        metadata["race_key"] = race_key
        metadata["raceKey"] = race_key
    return metadata


def _display_metric_value(value: Optional[int]) -> str:
    return "Onbekend" if value is None else str(value)


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
    excluded_weeks: Optional[ExcludedWeeks] = None,
) -> List[Dict[str, str]]:
    results: List[Dict[str, str]] = []
    excluded_weeks = excluded_weeks or set()

    for key, per_week_c in contrib_map.items():
        total_score = 0
        eligible = True

        for wh in season_weeks:
            if (key, wh) in excluded_weeks:
                continue
            c_val = per_week_c.get(wh)
            if c_val is None:
                if require_all_weekends:
                    eligible = False
                continue

            # Alleen meegerekend als Contribution > 0
            if c_val <= 0:
                if require_all_weekends:
                    eligible = False
                continue

            d_val = decks_map.get(key, {}).get(wh)
            if d_val is None or d_val != 16:
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


def row_total_for_weeks(
    week_values: Dict[str, Optional[int]],
    week_headers: List[str],
) -> Optional[int]:
    values = [week_values.get(wh) for wh in week_headers]
    if any(value is None for value in values):
        return None
    return sum(int(value) for value in values)


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

    contrib_map: Dict[str, Dict[str, Optional[int]]] = {}
    decks_map: Dict[str, Dict[str, Optional[int]]] = {}
    role_map: Dict[str, str] = {}
    player_print_map: Dict[str, str] = {}

    for r in contrib_rows2:
        player_print = r[0]
        key = clean_player_name(player_print)
        role = r[1] if len(r) > 1 else ""
        role_map[key] = role
        player_print_map[key] = player_print

        week_cells = r[c_idx + 1:]
        per_week: Dict[str, Optional[int]] = {}
        for wh, cell in zip(contrib_week_headers, week_cells):
            v = parse_int_cell(cell)
            per_week[wh] = v
        contrib_map[key] = per_week

    for r in decks_rows2:
        player_print = r[0]
        key = clean_player_name(player_print)
        player_print_map.setdefault(key, player_print)

        week_cells = r[d_idx + 1:]
        per_week: Dict[str, Optional[int]] = {}
        for wh, cell in zip(decks_week_headers, week_cells):
            v = parse_int_cell(cell)
            per_week[wh] = max(0, min(16, v)) if v is not None else None
        decks_map[key] = per_week

    return contrib_week_headers, decks_week_headers, contrib_map, decks_map, role_map, player_print_map


def compute_reliability_scores(
    contrib_map: Dict[str, Dict[str, Optional[int]]],
    decks_map: Dict[str, Dict[str, Optional[int]]],
    role_map: Dict[str, str],
    player_print_map: Dict[str, str],
    excluded_weeks: Optional[ExcludedWeeks] = None,
) -> List[Dict[str, object]]:
    results: List[Dict[str, object]] = []
    excluded_weeks = excluded_weeks or set()

    for key, per_week_c in contrib_map.items():
        weeks_played = 0
        excluded_week_count = 0
        missed_attacks = 0
        penalty_points = 0
        attacks_done = 0
        total_points = 0
        unknown_data = False

        for wh, c_val in per_week_c.items():
            if (key, wh) in excluded_weeks:
                if c_val is not None and c_val > 0:
                    excluded_week_count += 1
                continue
            if c_val is None:
                unknown_data = True
                continue
            if c_val <= 0:
                continue

            d_val = decks_map.get(key, {}).get(wh)
            if d_val is None:
                unknown_data = True
                continue
            weeks_played += 1
            total_points += c_val
            done = max(0, min(16, d_val))
            attacks_done += done
            missing = max(0, 16 - done)
            missed_attacks += missing
            penalty_points += UNREPLACEABLE_PENALTY.get(missing, missing * 4)

        total_possible = weeks_played * 16
        reliability_score = None
        avg_points = None
        if total_possible > 0 and not unknown_data:
            reliability_score = round((attacks_done / total_possible) * 100, 2)
            avg_points = round(total_points / weeks_played, 2)

        results.append(
            {
                "player": player_print_map.get(key, key),
                "player_tag": key,
                "role": role_map.get(key, ""),
                "weeks_played": weeks_played,
                "excluded_weeks": excluded_week_count,
                "attacks_done": attacks_done,
                "missed_attacks": missed_attacks,
                "penalty_points": penalty_points,
                "avg_points": avg_points,
                "reliability_score": reliability_score,
                "data_status": "partial" if unknown_data else "complete",
                "unknown_weeks": sum(
                    1
                    for wh, value in per_week_c.items()
                    if value is None and (key, wh) not in excluded_weeks
                ),
            }
        )

    results.sort(
        key=lambda r: (
            r.get("reliability_score") is not None,
            r.get("reliability_score") if r.get("reliability_score") is not None else -1,
            r.get("missed_attacks") if r.get("missed_attacks") is not None else -1,
        )
    )
    return results


ELDER_MIN_AVG_CONTRIB = 2500


def average_contribution(
    per_week_contrib: Dict[str, Optional[int]],
    week_headers: Optional[List[str]] = None,
) -> Optional[float]:
    values = (
        [per_week_contrib.get(week) for week in week_headers]
        if week_headers is not None
        else list(per_week_contrib.values())
    )
    if any(value is None for value in values):
        return None
    played_weeks = [v for v in values if v > 0]
    if not played_weeks:
        return None
    return round(sum(played_weeks) / len(played_weeks), 2)


def format_average_contribution(
    per_week_contrib: Dict[str, Optional[int]],
    week_headers: Optional[List[str]] = None,
) -> str:
    avg = average_contribution(per_week_contrib, week_headers)
    if avg is None:
        return "Onbekend"
    if avg.is_integer():
        return str(int(avg))
    return f"{avg:.2f}".rstrip("0").rstrip(".")


def build_promotion_candidates(
    contrib_map: Dict[str, Dict[str, Optional[int]]],
    decks_map: Dict[str, Dict[str, Optional[int]]],
    role_map: Dict[str, str],
    player_print_map: Dict[str, str],
    week_headers: List[str],
    evaluation_window: int = DEFAULT_PROMOTION_WINDOW,
    excluded_weeks: Optional[ExcludedWeeks] = None,
) -> List[Dict[str, object]]:
    suggestions: List[Dict[str, object]] = []
    excluded_weeks = excluded_weeks or set()
    evaluation_window = max(1, int(evaluation_window))

    for key, per_week_decks in decks_map.items():
        if (role_map.get(key, "") or "").strip().lower() != "member":
            continue

        included_weeks = [
            week
            for week in week_headers
            if (key, week) not in excluded_weeks
        ]
        if len(included_weeks) < evaluation_window:
            continue

        last_weeks = included_weeks[-evaluation_window:]
        if not all(per_week_decks.get(week) == 16 for week in last_weeks):
            continue

        streak = 0
        for week in reversed(included_weeks):
            if per_week_decks.get(week, 0) != 16:
                break
            streak += 1

        avg_score = average_contribution(
            contrib_map.get(key, {}),
            last_weeks,
        )
        if avg_score is None or avg_score < ELDER_MIN_AVG_CONTRIB:
            continue

        suggestions.append(
            {
                "player": player_print_map.get(key, key),
                "player_tag": key,
                "evaluation_window": evaluation_window,
                "streak_weeks": streak,
                "average_contribution": avg_score,
                "reason": (
                    f"Laatste {evaluation_window} meegetelde weken perfecte "
                    "attacks (D=16) als Member en Gem. C "
                    f"≥ {ELDER_MIN_AVG_CONTRIB}."
                ),
            }
        )

    suggestions.sort(
        key=lambda row: (
            row.get("streak_weeks", 0),
            row.get("average_contribution", 0),
        ),
        reverse=True,
    )
    return suggestions


def build_demotion_candidates(
    contrib_map: Dict[str, Dict[str, Optional[int]]],
    decks_map: Dict[str, Dict[str, Optional[int]]],
    role_map: Dict[str, str],
    player_print_map: Dict[str, str],
    week_headers: List[str],
    window: int = DEMOTION_WINDOW,
    max_missed_attacks: int = DEMOTION_MAX_MISSED_ATTACKS,
    excluded_weeks: Optional[ExcludedWeeks] = None,
) -> List[Dict[str, object]]:
    suggestions: List[Dict[str, object]] = []
    excluded_weeks = excluded_weeks or set()

    for key, per_week_contrib in contrib_map.items():
        if (role_map.get(key, "") or "").strip().lower() != "elder":
            continue

        played_weeks = [
            week
            for week in week_headers
            if (key, week) not in excluded_weeks
            and per_week_contrib.get(week) is not None
            and per_week_contrib.get(week) > 0
        ][-window:]
        if not played_weeks:
            continue

        if any(decks_map.get(key, {}).get(week) is None for week in played_weeks):
            continue

        missed_by_week = [
            max(
                0,
                16
                - max(
                    0,
                    min(16, decks_map.get(key, {}).get(week)),
                ),
            )
            for week in played_weeks
        ]
        missed_attacks = sum(missed_by_week)
        if missed_attacks <= max_missed_attacks:
            continue

        suggestions.append(
            {
                "player": player_print_map.get(key, key),
                "player_tag": key,
                "role": role_map.get(key, ""),
                "window_weeks": window,
                "observed_weeks": len(played_weeks),
                "missed_attacks": missed_attacks,
                "missed_weekends": sum(
                    1 for missed in missed_by_week if missed > 0
                ),
                "reason": (
                    f"In de laatste {len(played_weeks)} meegetelde gespeelde "
                    f"weken {missed_attacks} aanvallen gemist "
                    f"(grens: meer dan {max_missed_attacks} binnen "
                    f"{window} weken)."
                ),
            }
        )

    suggestions.sort(
        key=lambda row: (
            row.get("missed_attacks", 0),
            row.get("missed_weekends", 0),
        ),
        reverse=True,
    )
    return suggestions


def detect_current_and_previous_season(week_headers: List[str]) -> Tuple[Optional[int], Optional[int]]:
    seasons = sorted({s for s in (season_of_week_header(wh) for wh in week_headers) if s is not None})
    if not seasons:
        return None, None
    current = seasons[-1]
    prev = seasons[-2] if len(seasons) >= 2 else None
    return current, prev


def race_created_sort_value(race: Dict[str, object]) -> int:
    try:
        return int(
            clash_date_to_datetime(
                _race_value(race, "createdDate", "created_at", "race_created_at")
            ).timestamp()
        )
    except (TypeError, ValueError, OverflowError):
        return 0


def race_sort_key(race: Dict[str, object]) -> Tuple[int, int]:
    season = _safe_int(_race_value(race, "seasonId", "season_id")) or 0
    return (season, race_created_sort_value(race))


def dedupe_and_label_races(race_items: List[Dict[str, object]]) -> List[Tuple[str, Dict[str, object]]]:
    """
    Build logical week keys from chronological race order instead of raw sectionIndex.

    The Clash API's `sectionIndex` is not reliable for analytics history in practice:
    it can jump (e.g. `130-9`) and the current race can overlap with the latest race log
    entry, producing duplicates. We therefore:
    - sort chronologically by `(seasonId, createdDate)`
    - dedupe overlapping snapshots on `(seasonId, createdDate)`
    - assign week numbers sequentially within each season (`season-1`, `season-2`, ...)
    """

    ordered = sorted(
        (race for race in race_items if isinstance(race, dict)),
        key=race_sort_key,
    )
    deduped: List[Dict[str, object]] = []
    seen_keys: Set[Tuple[int, str]] = set()

    for race in ordered:
        season_id = _safe_int(_race_value(race, "seasonId", "season_id"))
        try:
            created_date = clash_date_to_iso(
                _race_value(race, "createdDate", "created_at", "race_created_at")
            )
        except (TypeError, ValueError, OverflowError):
            created_date = ""
        if season_id is None or season_id <= 0 or not created_date:
            continue
        dedupe_key = (
            season_id,
            created_date,
        )
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        deduped.append(race)

    labeled: List[Tuple[str, Dict[str, object]]] = []
    season_counts: Dict[int, int] = {}
    for race in deduped:
        season = _safe_int(_race_value(race, "seasonId", "season_id"))
        if season is None:
            continue
        season_counts[season] = season_counts.get(season, 0) + 1
        labeled.append((f"{season}-{season_counts[season]}", race))

    return labeled


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
            c_val = per_week_c.get(wh)
            if c_val is None:
                eligible = False
                break
            if c_val <= 0:
                eligible = False
                break
            d_val = decks_map.get(key, {}).get(wh)
            if d_val is None or d_val != 16:
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
            c_val = per_week_c.get(wh)
            if c_val is None:
                continue

            # Alleen "gespeeld weekend" als Contribution > 0
            if c_val <= 0:
                continue

            weeks_played += 1
            d_val = decks_map.get(key, {}).get(wh)

            # Perfect rule: als je speelt, dan moet je D=16 hebben
            if d_val is None or d_val != 16:
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
    clan_tag: str = DEFAULT_CLAN_TAG,
    strategy_weeks: Optional[object] = None,
    strategic_weeks: Optional[object] = None,
) -> Dict[str, object]:
    del analytics_url, members_url  # Legacy args; analytics now uses official Clash Royale API.
    if strategy_weeks is None:
        strategy_weeks = strategic_weeks

    api_key = os.environ.get("CLASH_ROYALE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing CLASH_ROYALE_API_KEY environment variable.")

    def api_get(path: str) -> Dict[str, object]:
        url = f"{ROYAL_API_BASE_URL}{path}"
        resp = requests.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=25)
        if resp.status_code != 200:
            raise RuntimeError(f"Clash API error {resp.status_code} for {path}")
        return resp.json() if resp.content else {}

    norm_tag = clan_tag.strip().replace("#", "").upper()
    encoded = f"%23{norm_tag}"

    members_payload = api_get(f"/clans/{encoded}/members")
    members = members_payload.get("items", [])

    role_map: Dict[str, str] = {}
    player_print_map: Dict[str, str] = {}
    known_players: Set[str] = set()
    for item in members:
        tag = (item.get("tag") or "").replace("#", "").upper()
        if not tag:
            continue
        known_players.add(tag)
        player_print_map[tag] = item.get("name") or tag
        role_map[tag] = item.get("role") or ""

    if not known_players:
        raise RuntimeError("No clan members returned by Clash API.")

    # Collect weekly race snapshots: historic races + current race when available.
    race_items: List[Dict[str, object]] = []
    river_log = api_get(f"/clans/{encoded}/riverracelog")
    race_items.extend(river_log.get("items", []))

    try:
        current_race = api_get(f"/clans/{encoded}/currentriverrace")
        if current_race:
            current_race["is_current"] = True
            race_items.append(current_race)
    except Exception:
        pass

    if not race_items:
        raise RuntimeError("No river race data returned by Clash API.")

    history_races, history_status = load_history_races_from_env(norm_tag)
    exclusion_rows, exclusion_status = load_week_exclusions_from_env(norm_tag)
    exclusions_by_snapshot: Dict[Tuple[str, str], str] = {}
    for row in exclusion_rows:
        try:
            race_created_at = clash_date_to_iso(row.get("race_created_at"))
        except (TypeError, ValueError, OverflowError):
            continue
        player_tag = normalize_tag(row.get("player_tag"))
        if not player_tag:
            continue
        exclusions_by_snapshot[(race_created_at, player_tag)] = str(
            row.get("reason") or ""
        )

    # Live races come first so an in-progress race replaces an older stored
    # copy with the same season/createdDate during deduplication.
    race_items.extend(history_races)

    labeled_races = dedupe_and_label_races(race_items)
    week_headers: List[str] = []
    week_metadata: Dict[str, Dict[str, object]] = {}
    week_evaluations: Dict[str, Dict[str, Dict[str, object]]] = {}
    excluded_weeks: ExcludedWeeks = set()
    contrib_map: Dict[str, Dict[str, Optional[int]]] = {}
    decks_map: Dict[str, Dict[str, Optional[int]]] = {}
    phase_statuses: Dict[str, int] = {}
    strategy_modes: Dict[str, int] = {}
    strategic_week_rows: List[Dict[str, object]] = []
    normal_analytics_excluded_weeks: List[str] = []
    metric_estimated_fields: Set[str] = set()

    for week_key, race in labeled_races:
        week_headers.append(week_key)
        race_created_at = clash_date_to_iso(
            _race_value(race, "createdDate", "created_at", "race_created_at")
        )
        phase_metadata = _race_phase_metadata(race)
        phase_status = phase_metadata.get("phase_status") or "not_available"
        phase_statuses[phase_status] = phase_statuses.get(phase_status, 0) + 1
        strategy_metadata = _strategy_week_for_race(
            race,
            week_key,
            norm_tag,
            strategy_weeks,
        )
        strategy_mode = str(strategy_metadata.get("strategy_mode") or "normal")
        strategy_modes[strategy_mode] = strategy_modes.get(strategy_mode, 0) + 1
        if strategy_metadata.get("is_strategic_week"):
            strategy_metadata = dict(strategy_metadata)
            strategic_week_rows.append(strategy_metadata)
        strategy_excluded = bool(
            strategy_metadata.get("is_strategic_week")
            and not strategy_metadata.get("included_in_normal_analytics")
        )
        if strategy_excluded:
            normal_analytics_excluded_weeks.append(week_key)
        strategy_exclusion_reason = (
            "Strategic experiment excluded from normal analytics"
            + (
                f": {strategy_metadata.get('reason')}"
                if strategy_metadata.get("reason")
                else "."
            )
            if strategy_excluded
            else ""
        )
        week_metadata[week_key] = {
            "season_id": _safe_int(_race_value(race, "seasonId", "season_id")),
            "race_created_at": race_created_at,
            **phase_metadata,
            "strategy_mode": strategy_mode,
            "strategyMode": strategy_mode,
            "strategic_week": strategy_metadata,
            "strategicWeek": strategy_metadata,
            "included_in_normal_analytics": strategy_metadata.get(
                "included_in_normal_analytics",
                True,
            ),
            "includedInNormalAnalytics": strategy_metadata.get(
                "includedInNormalAnalytics",
                True,
            ),
            "estimated": phase_metadata.get("phase_data_quality") == "estimated",
            "derived_fields": [],
            "estimated_fields": [],
        }
        source_rows = extract_clan_participants(race, norm_tag)

        for p in source_rows:
            ptag = normalize_tag(p.get("tag"))
            if not ptag or ptag not in known_players:
                continue

            player_print_map.setdefault(ptag, p.get("name") or ptag)
            role_map.setdefault(ptag, "")
            fame = _known_int(p, "fame")
            repair = _known_int(p, "repairPoints", "repair_points")
            contrib = fame + repair if fame is not None and repair is not None else None
            decks = _known_int(p, "decksUsed", "decks_used")
            if decks is not None:
                decks = max(0, min(16, decks))
            if contrib is not None:
                metric_estimated_fields.add("contribution")
                week_metadata[week_key]["derived_fields"] = ["contribution"]
                week_metadata[week_key]["estimated_fields"] = ["contribution"]

            contrib_map.setdefault(ptag, {})[week_key] = contrib
            decks_map.setdefault(ptag, {})[week_key] = decks
            explicit_exclusion_reason = exclusions_by_snapshot.get(
                (race_created_at, ptag),
                "",
            )
            exclusion_reasons = [
                reason
                for reason in (strategy_exclusion_reason, explicit_exclusion_reason)
                if reason
            ]
            is_included = not bool(exclusion_reasons)
            exclusion_reason = "; ".join(exclusion_reasons)
            if not is_included:
                excluded_weeks.add((ptag, week_key))
            week_evaluations.setdefault(ptag, {})[week_key] = {
                "included": is_included,
                "reason": exclusion_reason,
                "race_created_at": race_created_at,
                "phase_status": phase_metadata.get("phase_status"),
                "data_status": phase_metadata.get("data_status"),
                "estimated": phase_metadata.get("phase_data_quality") == "estimated",
                "derived_fields": ["contribution"] if contrib is not None else [],
                "estimated_fields": ["contribution"] if contrib is not None else [],
                "strategy_mode": strategy_mode,
                "strategic_week": strategy_metadata,
                "unknown_fields": [
                    field
                    for field, value in (
                        ("fame", fame),
                        ("repairPoints", repair),
                        ("decksUsed", decks),
                    )
                    if value is None
                ],
            }

        if strategy_excluded:
            for ptag in known_players:
                excluded_weeks.add((ptag, week_key))
                week_evaluations.setdefault(ptag, {}).setdefault(
                    week_key,
                    {
                        "included": False,
                        "reason": strategy_exclusion_reason,
                        "race_created_at": race_created_at,
                        "phase_status": phase_metadata.get("phase_status"),
                        "data_status": phase_metadata.get("data_status"),
                        "estimated": phase_metadata.get("phase_data_quality") == "estimated",
                        "derived_fields": [],
                        "estimated_fields": [],
                        "strategy_mode": strategy_mode,
                        "strategic_week": strategy_metadata,
                        "unknown_fields": [],
                    },
                )

    # Ensure all known players exist in maps for frontend stability.
    for ptag in known_players:
        contrib_map.setdefault(ptag, {})
        decks_map.setdefault(ptag, {})
        player_print_map.setdefault(ptag, ptag)
        role_map.setdefault(ptag, "")

    current_season, prev_season = detect_current_and_previous_season(week_headers)

    mvp_current: List[Dict[str, str]] = []
    if current_season is not None:
        weeks_current = [wh for wh in week_headers if season_of_week_header(wh) == current_season]
        mvp_current = compute_mvp_list(
            weeks_current,
            contrib_map,
            decks_map,
            player_print_map,
            top_n,
            require_all_weekends=False,
            excluded_weeks=excluded_weeks,
        )

    mvp_previous: List[Dict[str, str]] = []
    if prev_season is not None:
        weeks_prev = [wh for wh in week_headers if season_of_week_header(wh) == prev_season]
        mvp_previous = compute_mvp_list(
            weeks_prev,
            contrib_map,
            decks_map,
            player_print_map,
            top_n,
            require_all_weekends=True,
            excluded_weeks=excluded_weeks,
        )

    ratio_scores = compute_reliability_scores(
        contrib_map,
        decks_map,
        role_map,
        player_print_map,
        excluded_weeks=excluded_weeks,
    )
    promotion_candidates_by_window = {
        str(window): build_promotion_candidates(
            contrib_map,
            decks_map,
            role_map,
            player_print_map,
            week_headers,
            evaluation_window=window,
            excluded_weeks=excluded_weeks,
        )
        for window in PROMOTION_WINDOWS
    }
    promotion_candidates = promotion_candidates_by_window[
        str(DEFAULT_PROMOTION_WINDOW)
    ]
    demotion_candidates = build_demotion_candidates(
        contrib_map,
        decks_map,
        role_map,
        player_print_map,
        week_headers,
        excluded_weeks=excluded_weeks,
    )

    contrib_headers = ["Player", "Role", "C", "Avg C", *week_headers]
    decks_headers = ["Player", "Role", "D", *week_headers]
    ordered_players = sorted(player_print_map.items(), key=lambda item: item[1].lower())

    contrib_rows: List[List[str]] = []
    decks_rows: List[List[str]] = []
    for ptag, pname in ordered_players:
        role = role_map.get(ptag, "")
        per_week_contrib = contrib_map.get(ptag, {})
        per_week_decks = decks_map.get(ptag, {})
        included_headers = [
            week for week in week_headers if (ptag, week) not in excluded_weeks
        ]
        total_contrib = row_total_for_weeks(per_week_contrib, included_headers)
        total_decks = row_total_for_weeks(per_week_decks, included_headers)
        contrib_rows.append([
            pname,
            role,
            _display_metric_value(total_contrib),
            format_average_contribution(per_week_contrib, included_headers),
            *[_display_metric_value(per_week_contrib.get(wh)) for wh in week_headers],
        ])
        decks_rows.append([
            pname,
            role,
            _display_metric_value(total_decks),
            *[_display_metric_value(per_week_decks.get(wh)) for wh in week_headers],
        ])

    return {
        "mvp_current": mvp_current,
        "mvp_previous": mvp_previous,
        "current_season": current_season,
        "previous_season": prev_season,
        "ratio_scores": ratio_scores,
        "promotion_candidates": promotion_candidates,
        "promotion_candidates_by_window": promotion_candidates_by_window,
        "promotion_windows": list(PROMOTION_WINDOWS),
        "default_promotion_window": DEFAULT_PROMOTION_WINDOW,
        "demotion_candidates": demotion_candidates,
        "demotion_rule": {
            "window_weeks": DEMOTION_WINDOW,
            "max_missed_attacks": DEMOTION_MAX_MISSED_ATTACKS,
        },
        "contribution_table": {"headers": contrib_headers, "rows": contrib_rows},
        "decks_used_table": {"headers": decks_headers, "rows": decks_rows},
        "week_metadata": week_metadata,
        "week_evaluations": week_evaluations,
        "phase_statuses": phase_statuses,
        "strategy_modes": strategy_modes,
        "strategy_mode": (
            next(iter(strategy_modes))
            if len(strategy_modes) == 1
            else "mixed"
            if strategy_modes
            else "normal"
        ),
        "strategic_weeks": strategic_week_rows,
        "normal_analytics_excluded_weeks": sorted(set(normal_analytics_excluded_weeks)),
        "derived_fields": sorted(metric_estimated_fields),
        "estimated_fields": sorted(metric_estimated_fields),
        "history": {
            **history_status,
            "available_weeks": len(week_headers),
            "exclusions": exclusion_status,
            "phase_statuses": phase_statuses,
            "strategy_modes": strategy_modes,
            "strategy_mode": (
                next(iter(strategy_modes))
                if len(strategy_modes) == 1
                else "mixed"
                if strategy_modes
                else "normal"
            ),
            "strategic_weeks": strategic_week_rows,
            "normal_analytics_excluded_weeks": sorted(set(normal_analytics_excluded_weeks)),
            "estimated_fields": sorted(metric_estimated_fields),
        },
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
