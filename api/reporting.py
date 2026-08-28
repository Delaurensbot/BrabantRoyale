"""Pure official-data reporting helpers.

The old dashboard module mixed report formatting with HTML fetching/parsing.
These helpers accept normalized official API models or explicit report
metadata only. They never fetch, parse, or fall back to HTML.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Dict, List, Optional, Tuple


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ").strip())


def _context_value(context: object, name: str) -> object:
    if isinstance(context, Mapping):
        return context.get(name)
    return getattr(context, name, None)


def parse_day_number(context: object) -> Optional[int]:
    """Read a race day from explicit normalized/report metadata."""

    if isinstance(context, bool):
        return None
    if isinstance(context, int):
        return context if context in {1, 2, 3, 4} else None

    for name in ("day", "active_day", "period_index"):
        value = _context_value(context, name)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value in {1, 2, 3, 4}:
            return value
        if isinstance(value, str) and value.strip().isdigit():
            parsed = int(value.strip())
            if parsed in {1, 2, 3, 4}:
                return parsed

    if isinstance(context, str):
        match = re.fullmatch(r"\s*(?:Day|Dag)\s*([1-4])\s*", context)
        if match:
            return int(match.group(1))
    return None


def parse_day_label(context: object) -> Optional[str]:
    day = parse_day_number(context)
    return f"Day {day}" if day in {1, 2, 3, 4} else None


def translate_day_label(label: Optional[str]) -> Optional[str]:
    if not label:
        return None
    return label.replace("Day", "Dag", 1)


@dataclass
class ClanOverview:
    name: str
    decks_used_today: Optional[int]
    decks_total_today: Optional[int]
    avg_medals_per_deck: Optional[float]
    projected_medals: Optional[int]
    boat_points: Optional[int]
    current_medals: Optional[int]
    trophies: Optional[int]


def render_player_table(rows: List[Dict]) -> str:
    headers = ["#", "Name", "Role", "Today/Total", "Boat", "Medals"]

    def text(value: object) -> str:
        return str(value) if value is not None else ""

    name_width = max([len(text(row.get("name", ""))) for row in rows] + [len("Name")])
    role_width = max([len(text(row.get("role", ""))) for row in rows] + [len("Role")])
    deck_width = max(
        [
            len(f'{text(row.get("decks_used_today", ""))}/{text(row.get("decks_total_so_far", ""))}')
            for row in rows
        ]
        + [len("Today/Total")]
    )
    medal_width = max([len(text(row.get("fame", ""))) for row in rows] + [len("Medals")])

    head = (
        f'{headers[0]:>3} | {headers[1]:<{name_width}} | {headers[2]:<{role_width}} | '
        f'{headers[3]:>{deck_width}} | {headers[4]:>4} | {headers[5]:>{medal_width}}'
    )
    lines = [head, "-" * len(head)]
    for row in rows:
        decks = f'{text(row.get("decks_used_today", ""))}/{text(row.get("decks_total_so_far", ""))}'
        rank = row.get("rank")
        rank_text = str(rank) if rank is not None else ""
        lines.append(
            f'{rank_text:>3} | {text(row.get("name", "")):<{name_width}} | '
            f'{text(row.get("role", "")):<{role_width}} | {decks:>{deck_width}} | '
            f'{text(row.get("boat_attacks", "")):>4} | {text(row.get("fame", "")):>{medal_width}}'
        )
    return "\n".join(lines)


def attacks_left_today(row: Dict) -> Optional[int]:
    raw_used = row.get("decks_used_today")
    if raw_used in (None, ""):
        return None
    try:
        used = int(raw_used)
    except (TypeError, ValueError):
        return None
    return max(0, min(4, 4 - used))


def compute_battles_left(rows: List[Dict]) -> int:
    return sum(left for row in rows if (left := attacks_left_today(row)) is not None)


def compute_duels_left(rows: List[Dict]) -> int:
    return sum(
        1
        for row in rows
        if (left := attacks_left_today(row)) is not None and left >= 3
    )


def compute_total_players_participated(rows: List[Dict]) -> int:
    count = 0
    for row in rows:
        try:
            used_today = int(row.get("decks_used_today", 0) or 0)
        except (TypeError, ValueError):
            continue
        if used_today >= 1:
            count += 1
    return count


def bucket_open_players(rows: List[Dict]) -> Dict[int, List[str]]:
    buckets: Dict[int, List[str]] = {4: [], 3: [], 2: [], 1: [], 0: []}
    for row in rows:
        left = attacks_left_today(row)
        name = (row.get("name") or "").strip()
        if left is not None and name:
            buckets[left].append(name)
    return buckets


def render_battles_left_today(rows: List[Dict]) -> str:
    buckets = bucket_open_players(rows)
    lines = ["Battles left (today):"]
    known_players = sum(len(buckets[key]) for key in [4, 3, 2, 1, 0])
    any_added = False
    for count in [4, 3, 2, 1]:
        names = buckets[count]
        if not names:
            continue
        any_added = True
        lines.extend(["", f"{count} attack{'s' if count != 1 else ''} left:"])
        lines.extend(f"- {name}" for name in names)
    if known_players == 0:
        lines.extend(["", "Nog geen betrouwbare data voor 'decks used today' beschikbaar."])
    elif not any_added:
        lines.extend(["", "Iedereen is klaar voor vandaag."])
    return "\n".join(lines)


def render_risk_left_attacks(rows: List[Dict]) -> str:
    buckets = bucket_open_players(rows)
    lines = ["Spelers met nog losse aanvallen:"]
    any_added = False
    for count in [3, 2, 1]:
        names = buckets[count]
        if not names:
            continue
        any_added = True
        lines.extend(["", f"{count} attack{'s' if count != 1 else ''} left:"])
        lines.extend(f"- {name}" for name in names)
    if not any_added:
        lines.extend(["", "Geen risico spelers gevonden (niemand met 1-3 open)."])
    return "\n".join(lines)


def render_high_fame_players(
    context: object, rows: List[Dict], threshold: int = 3000
) -> str:
    if parse_day_number(context) != 4:
        return ""
    high_famers = []
    for row in rows:
        name = (row.get("name") or "").strip()
        try:
            fame = int(row.get("fame"))
        except (TypeError, ValueError):
            continue
        if name and fame >= threshold:
            high_famers.append((name, fame))
    high_famers.sort(key=lambda item: item[1], reverse=True)
    lines = ["Spelers 3000+ 🌟"]
    if not high_famers:
        lines.append("- Geen spelers boven de 3000 fame.")
        return "\n".join(lines)
    lines.extend([f"- Aantal: {len(high_famers)}", ""])
    lines.extend(f"- {name}: {fame}" for name, fame in high_famers)
    return "\n".join(lines)


def collect_day1_high_famers(
    context: object, rows: List[Dict], threshold: int = 800
) -> List[Tuple[str, int]]:
    if parse_day_number(context) != 1:
        return []
    high_famers = []
    for row in rows:
        name = (row.get("name") or "").strip()
        try:
            fame = int(row.get("fame"))
        except (TypeError, ValueError):
            continue
        if name and fame >= threshold:
            high_famers.append((name, fame))
    high_famers.sort(key=lambda item: item[1], reverse=True)
    return high_famers


def render_day1_high_fame_players(
    context: object, rows: List[Dict], threshold: int = 800
) -> str:
    high_famers = collect_day1_high_famers(context, rows, threshold)
    if not high_famers:
        return ""
    lines = ["Spelers 800+ 🏅", f"- Aantal: {len(high_famers)}", ""]
    lines.extend(f"- {name}: {fame}" for name, fame in high_famers)
    return "\n".join(lines)


def render_day4_last_chance_players(
    context: object, rows: List[Dict], min_fame: int = 2100
) -> str:
    if parse_day_number(context) != 4:
        return ""
    candidates = []
    for row in rows:
        name = (row.get("name") or "").strip()
        if attacks_left_today(row) != 4 or not name:
            continue
        try:
            fame = int(row.get("fame"))
        except (TypeError, ValueError):
            continue
        if fame >= min_fame:
            candidates.append((name, fame))
    candidates.sort(key=lambda item: item[1], reverse=True)
    lines = ["Spelers die nog 3k kunnen halen! 🌟"]
    if not candidates:
        lines.append("- Niemand gevonden met 0/4 en 2100+ punten.")
    else:
        lines.extend(f"- {name}: {fame}" for name, fame in candidates)
    return "\n".join(lines)


def render_clan_overview_table(clans: List[ClanOverview]) -> str:
    if not clans:
        return "Clan overview: (niet gevonden op deze pagina)"

    def text(value: object) -> str:
        return "" if value is None else str(value)

    def float_text(value: Optional[float]) -> str:
        return "" if value is None else f"{value:.2f}"

    for clan in clans:
        if clan.projected_medals is None and clan.avg_medals_per_deck is not None:
            clan.projected_medals = int(clan.avg_medals_per_deck * 200)

    name_width = max([len(clan.name) for clan in clans] + [len("Clan")])
    decks_width = max(
        [len(f"{text(clan.decks_used_today)}/{text(clan.decks_total_today)}") for clan in clans]
        + [len("Decks")]
    )
    avg_width = max([len(float_text(clan.avg_medals_per_deck)) for clan in clans] + [len("Avg/deck")])
    projected_width = max([len(text(clan.projected_medals)) for clan in clans] + [len("Projected")])
    boat_width = max([len(text(clan.boat_points)) for clan in clans] + [len("Boat")])
    medal_width = max([len(text(clan.current_medals)) for clan in clans] + [len("Medals")])
    head = (
        f'{"Clan":<{name_width}} | {"Decks":>{decks_width}} | {"Avg/deck":>{avg_width}} | '
        f'{"Projected":>{projected_width}} | {"Boat":>{boat_width}} | {"Medals":>{medal_width}}'
    )
    ordered = sorted(
        clans,
        key=lambda clan: (
            -(clan.current_medals if clan.current_medals is not None else -1),
            clan.name.lower(),
        ),
    )
    lines = ["Clan overview:", head, "-" * len(head)]
    for clan in ordered:
        decks = f"{text(clan.decks_used_today)}/{text(clan.decks_total_today)}"
        lines.append(
            f"{clan.name:<{name_width}} | {decks:>{decks_width}} | "
            f"{float_text(clan.avg_medals_per_deck):>{avg_width}} | "
            f"{text(clan.projected_medals):>{projected_width}} | "
            f"{text(clan.boat_points):>{boat_width}} | {text(clan.current_medals):>{medal_width}}"
        )
    return "\n".join(lines)


def render_clan_avg_projection(clans: List[ClanOverview]) -> str:
    if not clans:
        return "Clan avg/projection: (niet gevonden op deze pagina)"
    name_width = max([len(clan.name) for clan in clans] + [len("Clan name")])
    avg_width = max(
        [len(f"{clan.avg_medals_per_deck:.2f}") if clan.avg_medals_per_deck is not None else 0 for clan in clans]
        + [len("Avg")]
    )
    projected_width = max(
        [len(str(clan.projected_medals or "")) for clan in clans] + [len("Projected")]
    )
    header = f'{"Clan name":<{name_width}} | {"Avg":>{avg_width}} | {"Projected":>{projected_width}}'
    lines = ["Clan name avg projected:", header, "-" * len(header)]
    for clan in clans:
        avg = "" if clan.avg_medals_per_deck is None else f"{clan.avg_medals_per_deck:.2f}"
        projected = "" if clan.projected_medals is None else str(clan.projected_medals)
        lines.append(f"{clan.name:<{name_width}} | {avg:>{avg_width}} | {projected:>{projected_width}}")
    return "\n".join(lines)


def get_projected_ranking(clans: List[ClanOverview]) -> List[ClanOverview]:
    return sorted(
        [clan for clan in clans if clan.projected_medals is not None],
        key=lambda clan: int(clan.projected_medals),
        reverse=True,
    )


def find_our_clan(clans: List[ClanOverview], our_clan_name: str) -> Optional[ClanOverview]:
    target = our_clan_name.strip().lower()
    return next((clan for clan in clans if clan.name.strip().lower() == target), None)


def render_clan_insights(clans: List[ClanOverview], our_clan_name: str) -> str:
    if not clans:
        return "Insights: (geen clan overview beschikbaar)"
    lines = ["Insights:", "", "Clans finished (all decks used today):"]
    finished = [
        clan.name
        for clan in clans
        if clan.decks_used_today is not None
        and clan.decks_total_today is not None
        and clan.decks_used_today >= clan.decks_total_today
    ]
    if finished:
        lines.extend(f"- {name}" for name in finished)
    else:
        lines.append("- (nog niemand)")
    lines.extend(["", "Projected ranking (high to low):"])
    projected = get_projected_ranking(clans)
    if projected:
        lines.extend(
            f"{index:>2}. {clan.name} -> {clan.projected_medals}"
            for index, clan in enumerate(projected, 1)
        )
    else:
        lines.append("(projected medals niet gevonden)")

    our = find_our_clan(clans, our_clan_name)
    if (
        our
        and our.current_medals is not None
        and our.decks_used_today is not None
        and our.decks_total_today is not None
    ):
        remaining = max(0, int(our.decks_total_today) - int(our.decks_used_today))
        lines.extend(
            [
                "",
                f"Our clan: {our.name}",
                f"- Current medals: {our.current_medals}",
                f"- Decks used today: {our.decks_used_today}/{our.decks_total_today}",
                f"- Decks remaining today: {remaining}",
            ]
        )
        if remaining > 0 and our.projected_medals is not None:
            higher = [clan for clan in projected if clan.projected_medals > our.projected_medals]
            if higher:
                target = higher[-1]
                needed_total = int(target.projected_medals) + 1
                needed_average = (needed_total - int(our.current_medals)) / remaining
                lines.extend(
                    [
                        "",
                        "To beat the closest clan above us (by projected medals):",
                        f"- Target: {target.name} projected {target.projected_medals}",
                        f"- Needed average medals per remaining deck: {needed_average:.2f}",
                    ]
                )
            else:
                lines.extend(["", "We are not behind anyone on projected medals (or projected missing)."])
    return "\n".join(lines)


def render_clan_stats_block(
    context: object,
    clans: List[ClanOverview],
    our_clan_name: str,
    members_rows: List[Dict],
) -> str:
    our = find_our_clan(clans, our_clan_name)
    ranking = get_projected_ranking(clans)
    lines = ["Clan Stats:"]
    day = parse_day_label(context)
    if day:
        lines.append(f"- {day}")
    if our and our.avg_medals_per_deck is not None:
        lines.append(f"- Avg medals/deck: {our.avg_medals_per_deck:.2f}")
    lines.append(f"- Battles left: {compute_battles_left(members_rows)}")
    lines.append(f"- Duels left: {compute_duels_left(members_rows)}")
    lines.append(f"- Total players participated: {compute_total_players_participated(members_rows)}")
    if our and our.projected_medals is not None and ranking:
        position = next(
            (
                index
                for index, clan in enumerate(ranking, 1)
                if clan.name.strip().lower() == our.name.strip().lower()
            ),
            1,
        )
        lines.append(f"- Projected: {our.projected_medals} ({position}e)")
    if (
        our
        and our.current_medals is not None
        and our.decks_used_today is not None
        and our.decks_total_today is not None
    ):
        remaining = max(0, int(our.decks_total_today) - int(our.decks_used_today))
        lines.extend(
            [
                f"- Decks: {our.decks_used_today}/{our.decks_total_today} (open {remaining})",
                f"- Current medals: {our.current_medals}",
            ]
        )
    return "\n".join(lines)


def build_short_story(
    context: object,
    clans: List[ClanOverview],
    our_clan_name: str,
    members_rows: List[Dict],
    max_chars: int,
) -> str:
    del members_rows
    day_label = translate_day_label(parse_day_label(context))
    our = find_our_clan(clans, our_clan_name)
    ranking = get_projected_ranking(clans)
    position = None
    if our and our.projected_medals is not None and ranking:
        position = next(
            (
                index
                for index, clan in enumerate(ranking, 1)
                if clan.name.strip().lower() == our.name.strip().lower()
            ),
            None,
        )

    avg_sorted = sorted(
        [clan for clan in clans if clan.avg_medals_per_deck is not None],
        key=lambda clan: clan.avg_medals_per_deck or 0,
        reverse=True,
    )
    gap_line = ""
    if our and our.avg_medals_per_deck is not None:
        our_index = next(
            (
                index
                for index, clan in enumerate(avg_sorted)
                if clan.name.strip().lower() == our.name.strip().lower()
            ),
            None,
        )
        if our_index == 0 and len(avg_sorted) > 1:
            gap_line = (
                f"voorsprong op 2e plaats: "
                f"{our.avg_medals_per_deck - (avg_sorted[1].avg_medals_per_deck or 0):.2f}"
            )
        elif our_index is not None and our_index > 0:
            gap_line = (
                f"achterstand op 1e plaats: "
                f"{(avg_sorted[0].avg_medals_per_deck or 0) - our.avg_medals_per_deck:.2f}"
            )

    lines = [f"{day_label} update:" if day_label else "Dag update:"]
    if our and our.decks_used_today is not None and our.decks_total_today is not None:
        lines.append(f"{our.decks_used_today}/{our.decks_total_today} aanvallen")
    if position is not None:
        lines.append(f"voorspelde uitkomst: {position}e plek")
    if our and our.avg_medals_per_deck is not None:
        lines.append(f"Avg {our.avg_medals_per_deck:.2f} 🎖")
    if gap_line:
        lines.append(gap_line)
    story = "\n".join(lines).strip()
    return story if len(story) <= max_chars else story[: max(0, max_chars - 1)] + "…"


__all__ = [
    "ClanOverview",
    "attacks_left_today",
    "bucket_open_players",
    "build_short_story",
    "clean_text",
    "collect_day1_high_famers",
    "compute_battles_left",
    "compute_duels_left",
    "compute_total_players_participated",
    "find_our_clan",
    "get_projected_ranking",
    "parse_day_label",
    "parse_day_number",
    "render_battles_left_today",
    "render_clan_avg_projection",
    "render_clan_insights",
    "render_clan_overview_table",
    "render_clan_stats_block",
    "render_day1_high_fame_players",
    "render_day4_last_chance_players",
    "render_high_fame_players",
    "render_player_table",
    "render_risk_left_attacks",
    "translate_day_label",
]
