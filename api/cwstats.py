from http.server import BaseHTTPRequestHandler
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse
import re

from bs4 import BeautifulSoup

try:
    from api import strategy_engine
except ImportError:
    import strategy_engine

from Royale_api import (
    OUR_CLAN_NAME_DEFAULT,
    RACE_URL_DEFAULT,
    CLAN_URL_DEFAULT,
    build_short_story,
    ClanOverview,
    collect_day1_high_famers,
    compute_total_players_participated,
    dedupe_rows,
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
    value = str(raw or "").strip()
    compact_match = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*([KMB])", value, flags=re.IGNORECASE)
    if compact_match:
        number = float(compact_match.group(1).replace(",", "."))
        multiplier = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[compact_match.group(2).upper()]
        return int(number * multiplier)

    digits = re.sub(r"[^0-9]", "", value)
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

    if projected_rank is None:
        projected_rank, projected_finish = extract_rank_score(r"Placement\s*(\d+(?:st|nd|rd|th))\s*([\d.,]+)")
    if best_rank is None:
        best_rank, best_finish = extract_rank_score(r"Best\s*possible\s*(\d+(?:st|nd|rd|th))\s*([\d.,]+)")
    if worst_rank is None:
        worst_rank, worst_finish = extract_rank_score(r"Worst\s*possible\s*(\d+(?:st|nd|rd|th))\s*([\d.,]+)")

    battles_left = extract_number(r"Battles\s*Left\s*([\d.,]+)")
    if battles_left is None:
        decks_used_match = re.search(r"Decks\s*used\s*([\d.,]+)\s*/\s*([\d.,]+)", blob, flags=re.IGNORECASE)
        if decks_used_match:
            used = _compact_number(decks_used_match.group(1)) or 0
            total = _compact_number(decks_used_match.group(2)) or 0
            battles_left = max(0, total - used)

    duels_left = extract_number(r"Duels\s*Left\s*([\d.,]+)")
    if duels_left is None:
        slots_used_match = re.search(r"Slots\s*used\s*([\d.,]+)\s*/\s*([\d.,]+)", blob, flags=re.IGNORECASE)
        if slots_used_match:
            used = _compact_number(slots_used_match.group(1)) or 0
            total = _compact_number(slots_used_match.group(2)) or 0
            duels_left = max(0, total - used)

    return {
        "battles_left": battles_left,
        "duels_left": duels_left,
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


def _normalize_clan_name(name: str):
    cleaned = re.sub(r"\s+", " ", (name or "")).strip().lower()
    return re.sub(r"[^\w]+", "", cleaned)


def parse_cwstats_active_day(text: str):
    cleaned = re.sub(r"\bTraining\s+Day\s+1\s*-\s*3\b", " ", text or "", flags=re.IGNORECASE)
    war_match = re.search(r"\bday\s*([1-4])\s+war\b", cleaned, flags=re.IGNORECASE)
    if war_match:
        return int(war_match.group(1))

    matches = [
        int(match.group(1))
        for match in re.finditer(r"\bday\s*([1-4])\b", cleaned, flags=re.IGNORECASE)
    ]
    return matches[-1] if matches else None


def parse_cwstats_race_context_from_html(html: str):
    soup = BeautifulSoup(html or "", "html.parser")
    text_blob = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    text_blob_lower = text_blob.lower()

    is_colosseum_weekend = bool(re.search(r"\bcolosseum\b", text_blob_lower))

    active_day = parse_cwstats_active_day(text_blob)

    rows = {}
    row_regex = re.compile(r"^\s*(\d+)\s+(.*?)\s+(\d+)\s+(\d+)\s+(\d+)\s+([\d.,]+)\s*$")
    current_row_regex = re.compile(
        r"^\s*(\d+)\s+"
        r"(.+?)\s+"
        r"(?:\d+(?:[.,]\d+)?K\s+)?"
        r"([\d.,]+)\s+"
        r"([\d.,]+)\s+"
        r"([\d.,]+)\s+"
        r"([\d.,]+)\s*$",
        flags=re.IGNORECASE,
    )

    def _store_row(rank, name, trophy, boat_movement, cw_trophy, fame_avg):
        normalized_name = _normalize_clan_name(name)
        if not normalized_name:
            return
        rows[normalized_name] = {
            "rank": int(rank),
            "name": re.sub(r"\s+", " ", name).strip(),
            "trophy": _compact_number(str(trophy)) or 0,
            "cw_trophy": _compact_number(str(cw_trophy)) or 0,
            "fame": _compact_number(str(trophy)) or 0,
            "clan_war_trophies": _compact_number(str(cw_trophy)) or 0,
            "boat_movement": _compact_number(str(boat_movement)) or 0,
            "fame_avg": float(str(fame_avg).replace(",", ".")),
        }

    for link in soup.find_all("a", href=True):
        href = (link.get("href") or "").strip()
        if not re.fullmatch(r"/clan/[A-Z0-9]+/race", href):
            continue

        row_text = " ".join(link.stripped_strings)
        if not row_text or not row_text[0].isdigit():
            continue

        match = row_regex.match(row_text)
        if match:
            _store_row(*match.groups())
            continue

        match = current_row_regex.match(row_text)
        if match:
            rank, name, cw_trophy, boat_movement, fame, fame_avg = match.groups()
            _store_row(rank, name, fame, boat_movement, cw_trophy, fame_avg)

    if not rows:
        fallback_blob = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        fallback_regex = re.compile(
            r"(\d+)\s+"
            r"(.+?)\s+"
            r"([\d.,]+)\s+Clan\s*War\s*trophies\s+"
            r"([\d.,]+)\s+Boat\s*movement\s+"
            r"([\d.,]+)\s+Fame\s+"
            r"([\d.,]+)",
            flags=re.IGNORECASE,
        )
        for match in fallback_regex.finditer(fallback_blob):
            rank, name, cw_trophy, boat_movement, trophy, fame_avg = match.groups()
            _store_row(rank, name, trophy, boat_movement, cw_trophy, fame_avg)

    if not rows:
        def _to_int(value):
            if isinstance(value, bool) or value is None:
                return None
            if isinstance(value, (int, float)):
                return int(value)
            return _compact_number(str(value))

        def _to_float(value):
            if isinstance(value, bool) or value is None:
                return None
            if isinstance(value, (int, float)):
                return float(value)
            raw = str(value).strip().replace(",", ".")
            try:
                return float(raw)
            except ValueError:
                return None

        def _find_row_nodes(obj):
            found = []
            if isinstance(obj, dict):
                keys = {k.lower(): k for k in obj.keys()}
                name_key = next((keys[k] for k in ("name", "clanname", "clan_name") if k in keys), None)
                if name_key:
                    has_rank = any(k in keys for k in ("rank", "position", "place"))
                    has_cw = any(k in keys for k in ("clanwartrophies", "cw_trophy", "cw_trophies"))
                    has_fame = any(k in keys for k in ("fame", "famepoints", "currentfame", "score"))
                    if has_rank and has_cw and has_fame:
                        found.append(obj)
                for value in obj.values():
                    found.extend(_find_row_nodes(value))
            elif isinstance(obj, list):
                for item in obj:
                    found.extend(_find_row_nodes(item))
            return found

        for script in soup.find_all("script"):
            script_text = script.string or script.get_text("", strip=True)
            if not script_text or "{" not in script_text:
                continue

            candidates = []
            if script_text.strip().startswith("{"):
                candidates.append(script_text.strip())
            if "__NEXT_DATA__" in script_text:
                first_brace = script_text.find("{")
                last_brace = script_text.rfind("}")
                if first_brace != -1 and last_brace > first_brace:
                    candidates.append(script_text[first_brace:last_brace + 1])

            for candidate in candidates:
                try:
                    payload = json.loads(candidate)
                except Exception:
                    continue

                for node in _find_row_nodes(payload):
                    kl = {k.lower(): v for k, v in node.items()}
                    name = kl.get("name") or kl.get("clanname") or kl.get("clan_name")
                    rank = _to_int(kl.get("rank") or kl.get("position") or kl.get("place"))
                    cw_trophy = _to_int(kl.get("clanwartrophies") or kl.get("cw_trophy") or kl.get("cw_trophies"))
                    boat = _to_int(kl.get("boatmovement") or kl.get("boat_movement") or kl.get("boat")) or 0
                    fame = _to_int(kl.get("fame") or kl.get("famepoints") or kl.get("currentfame") or kl.get("score")) or 0
                    fame_avg = _to_float(kl.get("fameavg") or kl.get("fame_avg") or kl.get("avg") or kl.get("fameperdeck"))

                    if not name or rank is None or cw_trophy is None:
                        continue

                    _store_row(rank, str(name), fame, boat, cw_trophy, fame_avg or 0)

            if rows:
                break

    return {
        "is_colosseum_weekend": is_colosseum_weekend,
        "active_day": active_day,
        "rows_by_name": rows,
    }

def parse_cwstats_players_from_html(html: str):
    soup = BeautifulSoup(html or "", "html.parser")
    players = []

    def normalize_header(value):
        return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())

    header_indexes = {}
    for tr in soup.find_all("tr"):
        headers = [normalize_header(th.get_text(" ", strip=True)) for th in tr.find_all("th")]
        if headers:
            header_indexes = {header: index for index, header in enumerate(headers)}

    def idx_by(*names):
        normalized_names = {normalize_header(name) for name in names}
        for header, index in header_indexes.items():
            if header in normalized_names:
                return index
        return None

    idx_boat = idx_by("Boat movement", "Boat")
    idx_used_today = idx_by("Cards used today", "Decks used today", "Used today", "Today")
    idx_cards = idx_by("Cards", "Decks", "Decks used", "Total")
    idx_medals = idx_by("Medals", "Score", "Fame")

    for tr in soup.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(cells) < 6:
            continue

        rank_raw = (cells[0] or "").strip()
        if not re.fullmatch(r"\d+", rank_raw):
            continue

        name = (cells[1] or "").strip()
        if not name:
            continue

        boat_index = idx_boat if idx_boat is not None else 2
        used_index = idx_used_today if idx_used_today is not None else 3
        cards_index = idx_cards if idx_cards is not None else 4
        medals_index = idx_medals if idx_medals is not None else 5

        players.append({
            "rank": int(rank_raw),
            "tag": "",
            "name": name,
            "role": "",
            "boat_attacks": (_compact_number(cells[boat_index]) if boat_index < len(cells) else 0) or 0,
            "decks_used_today": (_compact_number(cells[used_index]) if used_index < len(cells) else 0) or 0,
            "decks_total_so_far": (_compact_number(cells[cards_index]) if cards_index < len(cells) else 0) or 0,
            "fame": (_compact_number(cells[medals_index]) if medals_index < len(cells) else 0) or 0,
        })

    return players


def pick_reporting_soup(race_soup: BeautifulSoup, cwstats_active_day):
    day_num = parse_day_number(race_soup)
    if day_num in {1, 2, 3, 4}:
        return race_soup

    if cwstats_active_day in {1, 2, 3, 4}:
        return BeautifulSoup(f"Day {cwstats_active_day}", "html.parser")

    return race_soup


def build_war_phase(day_num, cwstats_race_context):
    is_colosseum = bool((cwstats_race_context or {}).get("is_colosseum_weekend"))
    cwstats_day = (cwstats_race_context or {}).get("active_day")
    active_day = day_num if day_num in {1, 2, 3, 4} else cwstats_day
    source = "royaleapi" if day_num in {1, 2, 3, 4} else "cwstats" if active_day in {1, 2, 3, 4} else "unknown"
    mode = "colosseum" if is_colosseum else "river_race"
    mode_label = "Colosseum" if is_colosseum else "River Race"
    day_label = f"Dag {active_day}" if active_day in {1, 2, 3, 4} else "dag onbekend"

    return {
        "mode": mode,
        "day": active_day,
        "label": f"{mode_label} - {day_label}",
        "is_battle_day": active_day in {1, 2, 3, 4},
        "is_colosseum": is_colosseum,
        "source": source,
        "confidence": "high" if source == "royaleapi" else "medium" if source == "cwstats" else "low",
    }


def build_race_rows(clans):
    current_sorted = sorted(
        clans or [],
        key=lambda c: (c.current_medals if c.current_medals is not None else -1, c.name.lower()),
        reverse=True,
    )
    projected_sorted = sorted(
        clans or [],
        key=lambda c: (c.projected_medals if c.projected_medals is not None else -1, c.name.lower()),
        reverse=True,
    )
    current_rank_by_name = {_normalize_clan_name(c.name): index for index, c in enumerate(current_sorted, start=1)}
    projected_rank_by_name = {_normalize_clan_name(c.name): index for index, c in enumerate(projected_sorted, start=1)}

    rows = []
    for clan in current_sorted:
        key = _normalize_clan_name(clan.name)
        decks_used = clan.decks_used_today
        decks_total = clan.decks_total_today
        decks_remaining = None
        if decks_used is not None and decks_total is not None:
            decks_remaining = max(0, int(decks_total) - int(decks_used))

        rows.append({
            "name": clan.name,
            "current_rank": current_rank_by_name.get(key),
            "projected_rank": projected_rank_by_name.get(key),
            "decks": {
                "used": decks_used,
                "total": decks_total,
                "remaining": decks_remaining,
            },
            "avg": clan.avg_medals_per_deck,
            "projected": clan.projected_medals,
            "boat_points": clan.boat_points,
            "medals": clan.current_medals,
            "trophies": clan.trophies,
        })

    return rows


def _rank_for_score(race_rows, score, own_name):
    if score is None:
        return None
    better = 0
    own_key = _normalize_clan_name(own_name)
    for row in race_rows:
        if _normalize_clan_name(row.get("name")) == own_key:
            continue
        comparable = row.get("projected")
        if comparable is None:
            comparable = row.get("medals")
        if comparable is not None and comparable > score:
            better += 1
    return better + 1


def build_strategy(race_rows, war_phase, clan_name, finish_outlook):
    if not race_rows:
        return {
            "status": "Onvoldoende data",
            "recommendation": "Geen betrouwbaar strategieadvies beschikbaar.",
            "risk_level": "unknown",
            "needed_medals": None,
            "needed_avg_per_remaining_deck": None,
            "safe_boat_attack_budget": 0,
            "target_clan": None,
            "boat_targets": [],
            "copy_text": "Strategieadvies: onvoldoende data beschikbaar.",
        }

    own_key = _normalize_clan_name(clan_name)
    own = next((row for row in race_rows if _normalize_clan_name(row.get("name")) == own_key), None)
    if not own:
        return {
            "status": "Eigen clan niet gevonden",
            "recommendation": "Controleer clanselectie en race data.",
            "risk_level": "unknown",
            "needed_medals": None,
            "needed_avg_per_remaining_deck": None,
            "safe_boat_attack_budget": 0,
            "target_clan": None,
            "boat_targets": [],
            "copy_text": "Strategieadvies: eigen clan niet gevonden in Race Overview.",
        }

    if war_phase.get("is_colosseum"):
        copy_text = "Strategieadvies: Colosseum actief. Focus op medailles; bootaanvallen zijn niet relevant."
        return {
            "status": "Colosseum actief",
            "recommendation": "Focus op medailles. Bootaanvallen zijn in Colosseum niet relevant.",
            "risk_level": "colosseum",
            "needed_medals": None,
            "needed_avg_per_remaining_deck": None,
            "safe_boat_attack_budget": 0,
            "target_clan": None,
            "boat_targets": [],
            "copy_text": copy_text,
        }

    projected_rows = sorted(
        [row for row in race_rows if row.get("projected") is not None],
        key=lambda row: row.get("projected") or 0,
        reverse=True,
    )
    own_projected = own.get("projected")
    own_rank = _rank_for_score(race_rows, own_projected, own.get("name")) or own.get("projected_rank")
    own_remaining = (own.get("decks") or {}).get("remaining") or 0
    own_avg = own.get("avg") or 0

    if own_projected is None or not projected_rows:
        copy_text = "Strategieadvies: Race Overview is zichtbaar, maar projected data ontbreekt."
        return {
            "status": "Projected data ontbreekt",
            "recommendation": "Gebruik Race Overview en battles left; geen harde boot-target tonen.",
            "risk_level": "unknown",
            "needed_medals": None,
            "needed_avg_per_remaining_deck": None,
            "safe_boat_attack_budget": 0,
            "target_clan": None,
            "boat_targets": [],
            "copy_text": copy_text,
        }

    if own_rank and own_rank > 1:
        target = projected_rows[max(0, own_rank - 2)]
        needed_medals = max(0, (target.get("projected") or 0) - own_projected + 1)
        needed_avg = round(needed_medals / own_remaining, 2) if own_remaining else None
        copy_text = (
            f"Strategieadvies: focus medailles. Target: {target.get('name')} staat "
            f"{needed_medals} projected punten voor."
        )
        return {
            "status": "Achter op projected",
            "recommendation": "Focus op medailles. Bootaanvallen nu niet adviseren zolang we punten moeten inlopen.",
            "risk_level": "behind",
            "needed_medals": needed_medals,
            "needed_avg_per_remaining_deck": needed_avg,
            "safe_boat_attack_budget": 0,
            "target_clan": target.get("name"),
            "boat_targets": [],
            "copy_text": copy_text,
        }

    threat = next((row for row in projected_rows if _normalize_clan_name(row.get("name")) != own_key), None)
    margin = own_projected - (threat.get("projected") or 0) if threat else None
    buffer = 1000
    safe_budget = 0
    if margin is not None and own_avg:
        safe_budget = max(0, int((margin - buffer) // max(1, own_avg)))
        safe_budget = min(safe_budget, 8)

    boat_targets = []
    if threat and safe_budget > 0:
        boat_targets.append({
            "priority": 1,
            "clan": threat.get("name"),
            "reason": "Hoogste projected bedreiging onder ons.",
            "projected_gap": margin,
            "boat_points": threat.get("boat_points"),
            "medals": threat.get("medals"),
            "projected": threat.get("projected"),
        })

    if margin is None:
        status = "Geen directe bedreiging gevonden"
        recommendation = "Blijf medailles pakken; er is geen duidelijke boot-target."
        risk_level = "safe"
    elif margin <= buffer:
        status = "Voorsprong is krap"
        recommendation = "Focus op medailles. Bootaanvallen niet adviseren zolang de marge klein is."
        risk_level = "watch"
        safe_budget = 0
        boat_targets = []
    else:
        status = "Voorsprong op projected"
        recommendation = "Medailles blijven pakken. Bootaanvallen zijn optioneel als de lead ruim blijft."
        risk_level = "safe"

    copy_text = f"Strategieadvies: {status}. {recommendation}"
    if boat_targets:
        copy_text += f" Mogelijke boot-target: {boat_targets[0]['clan']}."

    return {
        "status": status,
        "recommendation": recommendation,
        "risk_level": risk_level,
        "needed_medals": 0,
        "needed_avg_per_remaining_deck": None,
        "safe_boat_attack_budget": safe_budget,
        "target_clan": threat.get("name") if threat else None,
        "boat_targets": boat_targets,
        "copy_text": copy_text,
    }


def pick_clan_config(path: str):
    parsed = urlparse(path)
    params = parse_qs(parsed.query)
    return get_clan_config(params.get("clan", [""])[0])


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            clan_config = pick_clan_config(self.path)
            warnings = []

            clan_html = ""
            clan_tags, clan_names = set(), set()
            clan_access_type = None
            try:
                clan_html = fetch_html(clan_config["clan_url"])
                clan_tags, clan_names = fetch_clan_members(clan_config["clan_url"], clan_html=clan_html)
                clan_access_type = parse_clan_access_type_from_html(clan_html)
            except Exception as clan_error:
                warnings.append(f"Kon clan pagina niet ophalen: {clan_error}")

            race_html = ""
            race_soup = BeautifulSoup("", "html.parser")
            day_num = None
            cw_official_started = False
            try:
                race_html = fetch_html(clan_config["race_url"])
                race_soup = BeautifulSoup(race_html, "html.parser")
                day_num = parse_day_number(race_soup)
                cw_official_started = day_num in {1, 2, 3, 4}
            except Exception as race_error:
                warnings.append(
                    "Kon race pagina niet ophalen. Dit kan een tijdelijke block/rate-limit of netwerkprobleem zijn: "
                    f"{race_error}"
                )

            cwstats_race_url = f"https://cwstats.com/clan/{clan_config.get('tag')}/race"
            cwstats_finish_outlook = {}
            cwstats_race_context = {}
            cwstats_players = []
            try:
                cwstats_html = fetch_html(cwstats_race_url)
                cwstats_finish_outlook = parse_cwstats_finish_outlook_from_html(cwstats_html)
                cwstats_race_context = parse_cwstats_race_context_from_html(cwstats_html)
                cwstats_players = parse_cwstats_players_from_html(cwstats_html)
            except Exception:
                cwstats_finish_outlook = {}
                cwstats_race_context = {}
                cwstats_players = []

            clans = parse_clan_overview_from_race_soup(race_soup)
            cwstats_rows = cwstats_race_context.get("rows_by_name") or {}

            if not clans and cwstats_rows:
                clans = [
                    ClanOverview(
                        name=str(row.get("name") or ""),
                        decks_used_today=None,
                        decks_total_today=None,
                        avg_medals_per_deck=row.get("fame_avg"),
                        projected_medals=int(float(row.get("fame_avg")) * 200) if row.get("fame_avg") is not None else None,
                        boat_points=row.get("boat_movement"),
                        current_medals=row.get("fame") or row.get("trophy"),
                        trophies=row.get("clan_war_trophies") or row.get("cw_trophy"),
                    )
                    for row in cwstats_rows.values()
                    if row.get("name")
                ]

            is_colosseum_weekend = bool(cwstats_race_context.get("is_colosseum_weekend"))
            for clan in clans:
                cw_row = cwstats_rows.get(_normalize_clan_name(clan.name))
                if not cw_row:
                    continue

                clan.avg_medals_per_deck = cw_row.get("fame_avg")
                if clan.boat_points in (None, 0):
                    clan.boat_points = cw_row.get("boat_movement")
                if clan.current_medals in (None, 0):
                    clan.current_medals = cw_row.get("fame") or cw_row.get("trophy")
                if clan.trophies in (None, 0):
                    clan.trophies = cw_row.get("clan_war_trophies") or cw_row.get("cw_trophy")

                if (
                    is_colosseum_weekend
                    and clan.current_medals in (None, 0)
                    and clan.boat_points not in (None, 0)
                ):
                    # During colosseum weekend the running score can appear in the
                    # Boat column on the official race page. Move that score to
                    # Medals so the overview ranking stays correct.
                    clan.current_medals = clan.boat_points

            players = parse_player_rows_from_race_soup(race_soup)

            filtered_players = []
            if clan_tags or clan_names:
                for row in players:
                    tag = (row.get("tag") or "").strip().upper()
                    name = (row.get("name") or "").strip()
                    if (tag and tag in clan_tags) or (name and name in clan_names):
                        filtered_players.append(row)
            else:
                filtered_players = list(players)

            if not filtered_players and cwstats_players:
                filtered_players = list(cwstats_players)

            filtered_players = sorted(filtered_players, key=lambda r: int(r.get("rank", 0) or 0))
            filtered_players = dedupe_rows(filtered_players)
            total_players_participated = compute_total_players_participated(filtered_players)

            war_phase = build_war_phase(day_num, cwstats_race_context)
            strategy_package = strategy_engine.build_strategy_package(
                clans,
                clan_config.get("name") or OUR_CLAN_NAME_DEFAULT,
                filtered_players,
                cwstats_finish_outlook,
                war_phase,
            )
            race_rows = strategy_package.get("raceRows", [])
            strategy = strategy_package.get("recommendation", {})

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
            reporting_soup = pick_reporting_soup(
                race_soup,
                cwstats_race_context.get("active_day"),
            )

            high_fame_text = render_high_fame_players(reporting_soup, filtered_players)
            day1_high_famers = collect_day1_high_famers(reporting_soup, filtered_players)
            day1_high_fame_text = render_day1_high_fame_players(
                reporting_soup, filtered_players
            )
            day4_last_chance_text = render_day4_last_chance_players(
                reporting_soup, filtered_players
            )
            short_story_limit = 220
            short_story_text = build_short_story(
                race_soup,
                clans,
                clan_config.get("name") or OUR_CLAN_NAME_DEFAULT,
                filtered_players,
                max_chars=short_story_limit,
            )

            if not clans and warnings and not cwstats_rows:
                race_overview_text = (
                    "Clan overview: race data nu niet beschikbaar via backend fetch. "
                    "Controleer netwerk/proxy/rate-limit en probeer opnieuw."
                )
                clan_stats_text = "\n".join(warnings)

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
                ("Strategieadvies", strategy.get("summary")),
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
                "war_phase": war_phase,
                "war_context": strategy_package.get("warContext"),
                "race_rows": race_rows,
                "strategy": strategy,
                "rank_targets": strategy_package.get("rankTargets"),
                "projections": strategy_package.get("projections"),
                "data_quality": strategy_package.get("dataQuality"),
                "cwstats_colosseum_weekend": bool(cwstats_race_context.get("is_colosseum_weekend")),
                "cwstats_active_day": war_phase.get("day"),
                "total_players_participated": total_players_participated,
                "clan_access_type": clan_access_type,
                "cw_official_started": cw_official_started,
                "warnings": warnings,
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
