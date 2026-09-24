from http.server import BaseHTTPRequestHandler
import json
import os
from urllib.parse import parse_qs, urlparse

import requests


ROYAL_API_BASE_URL = "https://proxy.royaleapi.dev/v1"
DEFAULT_CLAN_TAG = "9YP8UY"
ALLOWED_CLANS = {"9YP8UY", "GPCLVLPP", "RLQQQC99"}
MAX_DECKS_PER_PLAYER = 4
MAX_CLAN_DECKS_PER_DAY = 200
COLOSSEUM_PROJECTION_DAYS = 4
CLAN_CHAT_LIMIT = 250


def normalize_tag(raw_tag: str) -> str:
    cleaned = (raw_tag or "").replace("#", "").replace("%23", "")
    cleaned = "".join(ch for ch in cleaned if ch.isalnum())
    normalized = cleaned.upper()
    return normalized if normalized in ALLOWED_CLANS else DEFAULT_CLAN_TAG


def request_json(endpoint: str, api_key: str):
    response = requests.get(
        endpoint,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=20,
    )
    response.raise_for_status()
    return response.json() if response.content else {}


def int_value(value, default=0):
    try:
        return int(value or 0)
    except Exception:
        return default


def is_colosseum_race(race_data):
    period_type = str((race_data or {}).get("periodType") or "")
    normalized = "".join(ch for ch in period_type.lower() if ch.isalnum())
    return normalized == "colosseum"


def battle_day_for_race(race_data):
    try:
        period_index = int((race_data or {}).get("periodIndex"))
    except (TypeError, ValueError):
        return None
    if period_index < 0:
        return None
    day_in_section = period_index % 7
    return day_in_section - 2 if 3 <= day_in_section <= 6 else None


def normalize_clan_tag(value):
    return str(value or "").replace("#", "").upper()


def completed_points_for_clan(race_data, clan_tag, battle_day):
    """Return medals from completed battle days, or None when a required log is missing."""

    if battle_day == 1:
        return 0
    if battle_day not in {2, 3, 4}:
        return None

    try:
        current_period = int((race_data or {}).get("periodIndex"))
    except (TypeError, ValueError):
        return None

    expected_periods = set(range(current_period - (battle_day - 1), current_period))
    points_by_period = {}
    wanted_tag = normalize_clan_tag(clan_tag)

    for period_log in (race_data or {}).get("periodLogs", []) or []:
        try:
            period_index = int(period_log.get("periodIndex"))
        except (TypeError, ValueError):
            continue
        if period_index not in expected_periods:
            continue

        for item in period_log.get("items", []) or []:
            item_tag = normalize_clan_tag((item.get("clan") or {}).get("tag"))
            if item_tag == wanted_tag:
                points_by_period[period_index] = int_value(item.get("pointsEarned"))
                break

    if set(points_by_period) != expected_periods:
        return None
    return sum(points_by_period.values())


def build_overview_rows(clans, race_data=None):
    is_colosseum = is_colosseum_race(race_data)
    battle_day = battle_day_for_race(race_data)
    rows = []
    for row in clans:
        participants = row.get("participants", []) or []
        decks_used_today = sum(int_value(p.get("decksUsedToday")) for p in participants)
        decks_used_total = sum(int_value(p.get("decksUsed")) for p in participants)
        decks_total = MAX_CLAN_DECKS_PER_DAY
        decks_remaining_today = max(0, decks_total - decks_used_today)

        participant_fame = sum(int_value(participant.get("fame")) for participant in participants)
        participant_repair = sum(int_value(participant.get("repairPoints")) for participant in participants)
        cumulative_medals = participant_fame + participant_repair
        boat_points = int_value(row.get("fame")) + int_value(row.get("repairPoints"))

        if is_colosseum:
            # Colosseum score and participant deck usage accumulate over all
            # four battle days.
            fame = participant_fame
            repair = participant_repair
            medals = cumulative_medals
            average_decks = decks_used_total
            effective_day = battle_day if battle_day in {1, 2, 3, 4} else COLOSSEUM_PROJECTION_DAYS
            future_days = max(0, COLOSSEUM_PROJECTION_DAYS - effective_day)
            projection_decks_remaining = decks_remaining_today + (future_days * decks_total)
            score_scope = "colosseum_cumulative"
            score_available = True
        else:
            # Participant medals accumulate through the week. Period logs hold
            # pointsEarned for each completed battle day, so subtract those to
            # isolate the active day's medals. The clan-level fame fields are
            # river progress and belong in the Boat column, not Medals.
            completed_points = completed_points_for_clan(race_data, row.get("tag"), battle_day)
            score_available = completed_points is not None
            medals = max(0, cumulative_medals - completed_points) if score_available else None
            fame = medals
            repair = 0 if score_available else None
            average_decks = decks_used_today
            projection_decks_remaining = decks_remaining_today
            score_scope = "river_race_day"

        avg_per_deck = round((medals / average_decks), 2) if medals is not None and average_decks > 0 else None
        projected = (
            int(round(medals + (avg_per_deck * projection_decks_remaining)))
            if avg_per_deck is not None
            else None
        )

        rows.append(
            {
                "name": row.get("name", "-"),
                "tag": row.get("tag", "-"),
                "fame": fame,
                "repair_points": repair,
                "medals": medals,
                "cumulative_medals": cumulative_medals,
                "boat_points": 0 if is_colosseum else boat_points,
                "decks_used_today": decks_used_today,
                "decks_used_total": decks_used_total,
                "decks_total_today": decks_total,
                "decks_remaining_today": decks_remaining_today,
                "projection_decks_remaining": projection_decks_remaining,
                "avg_medals_per_deck": avg_per_deck,
                "projected_medals": projected,
                "score_scope": score_scope,
                "score_available": score_available,
            }
        )

    rows.sort(key=lambda r: (int_value(r.get("medals")), int_value(r.get("projected_medals"))), reverse=True)
    return rows


def build_players(member_items, participant_rows):
    participant_by_tag = {
        str(item.get("tag", "")).replace("#", ""): item
        for item in participant_rows
        if item.get("tag")
    }

    players = []
    for member in member_items:
        clean_tag = str(member.get("tag", "")).replace("#", "")
        participant = participant_by_tag.get(clean_tag, {})

        decks_used_today = int_value(participant.get("decksUsedToday"))
        decks_total_so_far = int_value(participant.get("decksUsed"))
        fame = int_value(participant.get("fame"))
        boat_attacks = int_value(participant.get("boatAttacks"))
        attacks_left = max(0, MAX_DECKS_PER_PLAYER - decks_used_today)

        players.append(
            {
                "name": member.get("name", ""),
                "tag": member.get("tag", ""),
                "role": member.get("role", ""),
                "trophies": int_value(member.get("trophies")),
                "fame": fame,
                "boat_attacks": boat_attacks,
                "decks_used_today": decks_used_today,
                "decks_total_so_far": decks_total_so_far,
                "attacks_left_today": attacks_left,
            }
        )

    players.sort(key=lambda p: (p.get("fame", 0), p.get("decks_used_today", 0)), reverse=True)
    return players


def build_finish_outlook(clan_tag, overview_rows, players, is_colosseum=False):
    ours = None
    for row in overview_rows:
        if str(row.get("tag", "")).replace("#", "") == clan_tag:
            ours = row
            break

    if not ours:
        return {}

    avg_values = [row.get("avg_medals_per_deck") for row in overview_rows if row.get("avg_medals_per_deck") is not None]
    min_avg = min(avg_values) if avg_values else 0
    max_avg = max(avg_values) if avg_values else 0

    current_medals = int_value(ours.get("medals"))
    remaining_decks = int_value(ours.get("projection_decks_remaining"))
    score_available = ours.get("projected_medals") is not None
    projected_finish = int_value(ours.get("projected_medals")) if score_available else None
    best_finish = int(round(current_medals + (remaining_decks * max_avg))) if score_available else None
    worst_finish = int(round(current_medals + (remaining_decks * min_avg))) if score_available else None

    def rank_for(score: int):
        better = sum(
            1
            for row in overview_rows
            if str(row.get("tag", "")).replace("#", "") != clan_tag
            and int_value(row.get("projected_medals")) > score
        )
        return better + 1

    battles_left = sum(int_value(p.get("attacks_left_today")) for p in players)
    duels_left = sum(1 for p in players if int_value(p.get("attacks_left_today")) >= 3)
    total_players_participated = sum(1 for p in players if int_value(p.get("decks_used_today")) >= 1)

    return {
        "battles_left": battles_left,
        "duels_left": duels_left,
        "total_players_participated": total_players_participated,
        "projected_rank": rank_for(projected_finish) if score_available else None,
        "projected_finish": projected_finish,
        "best_rank": rank_for(best_finish) if score_available else None,
        "best_finish": best_finish,
        "worst_rank": rank_for(worst_finish) if score_available else None,
        "worst_finish": worst_finish,
        "score_available": score_available,
        "model": "official_api_derived",
        "projection_scope": "colosseum_remaining_days" if is_colosseum else "river_race_day",
    }


def format_integer_nl(value):
    if value is None:
        return "-"
    return f"{int(round(float(value))):,}".replace(",", ".")


def format_decimal_nl(value, digits=2):
    if value is None:
        return "-"
    return f"{float(value):.{digits}f}".replace(".", ",")


def ranked_rows(overview_rows, field):
    available = [row for row in overview_rows if row.get(field) is not None]
    return sorted(
        available,
        key=lambda row: (float(row.get(field)), str(row.get("name") or "").lower()),
        reverse=True,
    )


def rank_for_clan(rows, clan_tag):
    wanted_tag = normalize_clan_tag(clan_tag)
    for index, row in enumerate(rows, start=1):
        if normalize_clan_tag(row.get("tag")) == wanted_tag:
            return index
    return None


def build_projection_share_text(clan_tag, overview_rows):
    if not overview_rows:
        return "Geen racedata beschikbaar."

    is_colosseum = any(row.get("score_scope") == "colosseum_cumulative" for row in overview_rows)
    heading = "Colosseum: gem./aanval → eindscore" if is_colosseum else "Vandaag: gem./aanval → eindscore"
    ordered = ranked_rows(overview_rows, "projected_medals")
    unavailable = [row for row in overview_rows if row.get("projected_medals") is None]
    rows = [*ordered, *unavailable]
    names = [str(row.get("name") or "-").strip() for row in rows]

    def render():
        lines = [heading]
        for rank, (row, name) in enumerate(zip(rows, names), start=1):
            place = f"{rank}." if row.get("projected_medals") is not None else "-."
            lines.append(
                f"{place} {name}: "
                f"{format_decimal_nl(row.get('avg_medals_per_deck'))} → "
                f"{format_integer_nl(row.get('projected_medals'))}"
            )
        return "\n".join(lines)

    text = render()
    while len(text) > CLAN_CHAT_LIMIT and any(len(name) > 1 for name in names):
        index = max(range(len(names)), key=lambda i: len(names[i]))
        name = names[index].removesuffix("…")
        names[index] = name[:-2].rstrip() + "…" if len(name) > 2 else name[:1]
        text = render()
    return text


def build_short_story_text(clan_tag, overview_rows):
    wanted_tag = normalize_clan_tag(clan_tag)
    ours = next(
        (row for row in overview_rows if normalize_clan_tag(row.get("tag")) == wanted_tag),
        None,
    )
    if not ours:
        return "Geen eigen clanrij gevonden in de officiële racedata."
    if ours.get("projected_medals") is None:
        return "De officiële daglogs zijn nog niet compleet; daardoor kan de stand nog niet betrouwbaar worden samengevat."

    score_ranking = ranked_rows(overview_rows, "medals")
    projected_ranking = ranked_rows(overview_rows, "projected_medals")
    current_rank = rank_for_clan(score_ranking, clan_tag)
    projected_rank = rank_for_clan(projected_ranking, clan_tag)

    name = str(ours.get("name") or "Onze clan")
    if len(name) > 28:
        name = name[:27].rstrip() + "…"
    used = int_value(ours.get("decks_used_today"))
    total = int_value(ours.get("decks_total_today"))
    remaining = max(0, total - used)
    our_average = float(ours.get("avg_medals_per_deck") or 0)
    scope = "Colosseum" if ours.get("score_scope") == "colosseum_cumulative" else "Vandaag"
    sentences = [
        f"{name}: {current_rank}e {scope.lower()}. Verwacht: {projected_rank}e met "
        f"{format_integer_nl(ours.get('projected_medals'))} punten.",
        f"Nog {remaining} van {total} aanvallen; gemiddeld {format_decimal_nl(our_average)} punten per aanval.",
    ]

    if projected_rank == 1 and len(projected_ranking) > 1:
        threat = projected_ranking[1]
        lead = int_value(ours.get("projected_medals")) - int_value(threat.get("projected_medals"))
        opponent = str(threat.get("name") or "nummer 2")[:28]
        sentences.append(f"Verwachte voorsprong op {opponent}: {format_integer_nl(lead)} punten.")
    elif projected_rank and projected_rank > 1:
        target = projected_ranking[projected_rank - 2]
        target_score = int_value(target.get("projected_medals"))
        needed_points = max(0, target_score + 1 - int_value(ours.get("medals")))
        opponent = str(target.get("name") or "de clan erboven")[:28]
        if remaining > 0:
            required_average = needed_points / remaining
            sentences.append(
                f"Voor plek {projected_rank - 1} boven {opponent}: "
                f"{format_integer_nl(needed_points)} punten nodig uit {remaining} aanvallen "
                f"({format_decimal_nl(required_average, 1)} per aanval)."
            )
        else:
            gap = max(0, target_score - int_value(ours.get("projected_medals")))
            sentences.append(
                f"Geen aanvallen meer; verwacht {format_integer_nl(gap)} punten achter {opponent}."
            )

    story = " ".join(sentences)
    if len(story) <= CLAN_CHAT_LIMIT:
        return story
    # Preserve the result and comparison as complete sentences if a long clan name takes space.
    story = " ".join([sentences[0], *sentences[2:]])
    if len(story) <= CLAN_CHAT_LIMIT:
        return story
    return f"{name}: verwacht {projected_rank}e met {format_integer_nl(ours.get('projected_medals'))} punten."


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        api_key = os.environ.get("CLASH_ROYALE_API_KEY")
        if not api_key:
            return self._send_json(
                500,
                {"ok": False, "error": "Missing CLASH_ROYALE_API_KEY environment variable."},
            )

        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        clan_tag = normalize_tag(params.get("clan", [DEFAULT_CLAN_TAG])[0])
        encoded_tag = f"%23{clan_tag}"

        try:
            clan_data = request_json(f"{ROYAL_API_BASE_URL}/clans/{encoded_tag}", api_key)
            member_data = request_json(f"{ROYAL_API_BASE_URL}/clans/{encoded_tag}/members", api_key)
            race_data = request_json(f"{ROYAL_API_BASE_URL}/clans/{encoded_tag}/currentriverrace", api_key)

            members = member_data.get("items", []) if isinstance(member_data, dict) else []
            race_clans = race_data.get("clans", []) if isinstance(race_data, dict) else []
            own_clan = race_data.get("clan", {}) if isinstance(race_data, dict) else {}
            participant_rows = own_clan.get("participants", []) if isinstance(own_clan, dict) else []

            is_colosseum = is_colosseum_race(race_data)
            battle_day = battle_day_for_race(race_data)
            overview_rows = build_overview_rows(race_clans, race_data)
            players = build_players(members, participant_rows)
            finish_outlook = build_finish_outlook(
                clan_tag,
                overview_rows,
                players,
                is_colosseum,
            )
            projection_share_text = build_projection_share_text(clan_tag, overview_rows)
            short_story_text = build_short_story_text(clan_tag, overview_rows)

            is_open = str(clan_data.get("type", "")).lower() == "open"

            return self._send_json(
                200,
                {
                    "ok": True,
                    "clan_tag": clan_tag,
                    "clan": {
                        "name": clan_data.get("name", ""),
                        "tag": clan_data.get("tag", ""),
                        "type": clan_data.get("type", ""),
                        "members": int_value(clan_data.get("members")),
                        "clan_score": int_value(clan_data.get("clanScore")),
                        "war_trophies": int_value(clan_data.get("clanWarTrophies")),
                        "description": clan_data.get("description", ""),
                    },
                    "race_state": {
                        "section_index": race_data.get("sectionIndex"),
                        "period_index": race_data.get("periodIndex"),
                        "period_type": race_data.get("periodType"),
                        "battle_day": battle_day,
                        "is_colosseum_weekend": is_colosseum,
                        "score_scope": "colosseum_cumulative" if is_colosseum else "river_race_day",
                        "participants": len(participant_rows),
                        "decks_used_today": sum(int_value(p.get("decksUsedToday")) for p in participant_rows),
                    },
                    "overview_rows": overview_rows,
                    "players": players,
                    "finish_outlook": finish_outlook,
                    "projection_share_text": projection_share_text,
                    "short_story_text": short_story_text,
                    "is_open_clan": is_open,
                    "gaps": {
                        "high_fame_day_cards": "not_directly_available",
                        "cwstats_finish_model": "replaced_with_local_estimate",
                        "colosseum_context": "period_type",
                    },
                },
            )
        except requests.HTTPError as http_err:
            status = http_err.response.status_code if http_err.response is not None else 502
            return self._send_json(status, {"ok": False, "error": f"Upstream API error ({status})."})
        except requests.RequestException:
            return self._send_json(502, {"ok": False, "error": "Failed to contact RoyaleAPI proxy."})
        except Exception:
            return self._send_json(500, {"ok": False, "error": "Unexpected server error."})

    def _send_json(self, status_code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
