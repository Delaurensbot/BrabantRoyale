from http.server import BaseHTTPRequestHandler
from collections.abc import Mapping
from dataclasses import dataclass, field
import json
from datetime import datetime, timezone
import os
from urllib.parse import parse_qs, urlparse
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

try:
    from api import strategy_engine
except ImportError:
    import strategy_engine

from Royale_api import (
    OUR_CLAN_NAME_DEFAULT,
    build_short_story,
    ClanOverview,
    collect_day1_high_famers,
    compute_total_players_participated,
    fetch_html,
    get_clan_config,
    parse_day_number,
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

try:
    from api.clash_client import ClashClientError, ClashRoyaleClient, normalize_tag
    from api.clash_normalizers import (
        DATA_STATUS_EMPTY,
        DATA_STATUS_ERROR,
        DATA_STATUS_FRESH,
        DATA_STATUS_INVALID,
        DATA_STATUS_PARTIAL,
        DATA_STATUS_STALE,
        NormalizedRiverRace,
        PlayerProfile,
        RaceClan,
        RaceContext,
        RaceParticipant,
        normalize_clan,
        normalize_current_river_race,
        normalize_members,
    )
except ImportError:  # pragma: no cover - convenient when run as a loose file.
    from clash_client import ClashClientError, ClashRoyaleClient, normalize_tag
    from clash_normalizers import (
        DATA_STATUS_EMPTY,
        DATA_STATUS_ERROR,
        DATA_STATUS_FRESH,
        DATA_STATUS_INVALID,
        DATA_STATUS_PARTIAL,
        DATA_STATUS_STALE,
        NormalizedRiverRace,
        PlayerProfile,
        RaceClan,
        RaceContext,
        RaceParticipant,
        normalize_clan,
        normalize_current_river_race,
        normalize_members,
    )


OFFICIAL_SOURCE = "royaleapi_proxy"
HTML_FALLBACK_SOURCE = "cwstats_html_fallback"
HTML_FALLBACK_ENV = "CWSTATS_ENABLE_HTML_FALLBACK"

_OFFICIAL_ENDPOINTS = {
    "clan": "/clans/{tag}",
    "members": "/clans/{tag}/members",
    "race": "/clans/{tag}/currentriverrace",
}
_SAFE_CODES = frozenset(
    {
        "clash_client_error",
        "configuration_error",
        "invalid_tag",
        "invalid_request",
        "empty_response",
        "invalid_response",
        "invalid_json",
        "transport_error",
        "timeout",
        "bad_request",
        "authentication_error",
        "forbidden",
        "not_found",
        "rate_limited",
        "upstream_server_error",
        "unexpected_status",
        "normalization_error",
        "endpoint_error",
        "server_error",
    }
)


@dataclass
class _OfficialSnapshot:
    """Normalized official responses plus safe endpoint diagnostics."""

    clan_response: object = None
    members_response: object = None
    race_response: object = None
    clan: RaceClan | None = None
    members: tuple[PlayerProfile, ...] = ()
    race: NormalizedRiverRace | None = None
    endpoint_errors: dict[str, dict] = field(default_factory=dict)
    normalization_errors: dict[str, dict] = field(default_factory=dict)


@dataclass
class _HtmlFallback:
    """Temporary, opt-in fallback data; never the normal cwstats source."""

    finish_outlook: dict = field(default_factory=dict)
    race_context: dict = field(default_factory=dict)
    players: list[dict] = field(default_factory=list)
    clans: list[ClanOverview] = field(default_factory=list)


class _OfficialReportContext:
    """Small non-HTML context adapter for legacy renderers."""

    def __init__(self, day: int | None):
        self._text = f"Day {day}" if day in {1, 2, 3, 4} else ""

    def get_text(self, *_args, **_kwargs):
        return self._text


def _safe_int(value):
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


def _safe_float(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            result = float(value.strip().replace(",", "."))
        except (TypeError, ValueError, OverflowError):
            return None
        return result if result == result and abs(result) != float("inf") else None
    return None


def _safe_timestamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_endpoint(value, fallback=""):
    if not isinstance(value, str) or not value.strip():
        return fallback
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
        if parsed.scheme or parsed.netloc:
            return parsed.path or "/"
    except ValueError:
        return fallback
    return raw.split("?", 1)[0].split("#", 1)[0]


def _safe_code(value, fallback="endpoint_error"):
    if isinstance(value, str) and value.strip() in _SAFE_CODES:
        return value.strip()
    return fallback


def _metadata_value(response, key, default=None):
    metadata = None
    if isinstance(response, Mapping):
        metadata = response.get("metadata")
    if metadata is None:
        metadata = getattr(response, "metadata", None)
    if isinstance(metadata, Mapping):
        return metadata.get(key, default)
    return getattr(metadata, key, default) if metadata is not None else default


def _response_payload(response):
    if response is None:
        return None
    if isinstance(response, Mapping) and "metadata" in response and (
        "data" in response or "payload" in response
    ):
        return response.get("data", response.get("payload"))
    if hasattr(response, "data") and hasattr(response, "metadata"):
        try:
            return response.data
        except Exception:
            return None
    return response


def _response_status(response):
    status = _metadata_value(response, "data_status")
    if isinstance(status, str) and status.strip():
        return status.strip().lower()
    payload = _response_payload(response)
    if payload is None or (isinstance(payload, (Mapping, list, tuple)) and not payload):
        return DATA_STATUS_EMPTY
    if isinstance(payload, (Mapping, list, tuple)):
        return DATA_STATUS_FRESH
    return DATA_STATUS_INVALID


def _response_fetched_at(response):
    return _safe_timestamp(_metadata_value(response, "fetched_at"))


def _response_stale(response):
    value = _metadata_value(response, "is_stale", _metadata_value(response, "stale", False))
    return value if isinstance(value, bool) else False


def _safe_endpoint_error(error, expected_endpoint):
    if isinstance(error, ClashClientError):
        code = getattr(error, "code", "clash_client_error")
        status_code = _safe_int(getattr(error, "status_code", None))
        attempts = _safe_int(getattr(error, "attempts", None))
        endpoint = _safe_endpoint(getattr(error, "endpoint", None), expected_endpoint)
    else:
        code = "endpoint_error"
        status_code = None
        attempts = None
        endpoint = expected_endpoint
    return {
        "code": _safe_code(code),
        "status_code": status_code,
        "attempts": attempts,
        "endpoint": endpoint,
    }


def _endpoint_path(name, clan_tag):
    return _OFFICIAL_ENDPOINTS[name].format(tag=f"%23{clan_tag}")


def _endpoint_record(name, clan_tag, response=None, error=None):
    endpoint = _endpoint_path(name, clan_tag)
    if error is not None:
        return {
            "name": name,
            "endpoint": endpoint,
            "source": OFFICIAL_SOURCE,
            "fetched_at": None,
            "data_status": DATA_STATUS_ERROR,
            "is_stale": False,
            "stale": False,
            "stale_reason": None,
            "error_code": error.get("code"),
            "status_code": error.get("status_code"),
            "attempts": error.get("attempts"),
        }

    stale = _response_stale(response)
    status = _response_status(response)
    if stale:
        status = DATA_STATUS_STALE
    return {
        "name": name,
        "endpoint": _safe_endpoint(_metadata_value(response, "endpoint"), endpoint),
        "source": OFFICIAL_SOURCE,
        "fetched_at": _response_fetched_at(response),
        "data_status": status,
        "is_stale": stale,
        "stale": stale,
        "stale_reason": _safe_code(
            _metadata_value(response, "stale_reason"), ""
        ) or None,
        "error_code": _safe_code(_metadata_value(response, "error_code"), "") or None,
        "status_code": _safe_int(_metadata_value(response, "status_code")),
        "attempts": _safe_int(_metadata_value(response, "attempts")),
    }


def _call_official(client, method_name, clan_tag, endpoint_name):
    try:
        return getattr(client, method_name)(clan_tag), None
    except Exception as error:
        expected = _endpoint_path(endpoint_name, clan_tag)
        return None, _safe_endpoint_error(error, expected)


def fetch_official_snapshot(clan_tag, client=None):
    """Fetch and normalize the three official current-war responses.

    Each call is independent so a single upstream failure cannot turn the
    other endpoints into synthetic zero-valued data.  ``client`` is optional
    specifically to keep this boundary easy to inject in route tests.
    """

    normalized_tag = normalize_tag(clan_tag)
    clash_client = client if client is not None else ClashRoyaleClient()
    snapshot = _OfficialSnapshot()

    snapshot.clan_response, error = _call_official(
        clash_client, "get_clan", normalized_tag, "clan"
    )
    if error is not None:
        snapshot.endpoint_errors["clan"] = error

    snapshot.members_response, error = _call_official(
        clash_client, "get_members", normalized_tag, "members"
    )
    if error is not None:
        snapshot.endpoint_errors["members"] = error

    snapshot.race_response, error = _call_official(
        clash_client, "get_current_river_race", normalized_tag, "race"
    )
    if error is not None:
        snapshot.endpoint_errors["race"] = error

    if snapshot.clan_response is not None:
        try:
            snapshot.clan = normalize_clan(
                snapshot.clan_response,
                clan_tag=normalized_tag,
            )
        except Exception:
            snapshot.normalization_errors["clan"] = {
                "code": "normalization_error",
                "endpoint": _endpoint_path("clan", normalized_tag),
                "status_code": None,
                "attempts": None,
            }

    if snapshot.members_response is not None:
        try:
            snapshot.members = normalize_members(
                snapshot.members_response,
                clan_tag=normalized_tag,
            )
        except Exception:
            snapshot.normalization_errors["members"] = {
                "code": "normalization_error",
                "endpoint": _endpoint_path("members", normalized_tag),
                "status_code": None,
                "attempts": None,
            }

    if snapshot.race_response is not None:
        try:
            snapshot.race = normalize_current_river_race(
                snapshot.race_response,
                clan_tag=normalized_tag,
                members=snapshot.members,
            )
        except Exception:
            snapshot.normalization_errors["race"] = {
                "code": "normalization_error",
                "endpoint": _endpoint_path("race", normalized_tag),
                "status_code": None,
                "attempts": None,
            }

    return snapshot



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


def _copy_clan_overviews(clans):
    """Copy legacy adapters before renderers add inferred display values."""

    return [
        ClanOverview(
            name=clan.name,
            decks_used_today=clan.decks_used_today,
            decks_total_today=clan.decks_total_today,
            avg_medals_per_deck=clan.avg_medals_per_deck,
            projected_medals=clan.projected_medals,
            boat_points=clan.boat_points,
            current_medals=clan.current_medals,
            trophies=clan.trophies,
        )
        for clan in clans or []
    ]


def _known_decks_used(participants):
    """Sum participant decks only when every visible value is known."""

    if not participants:
        return None
    values = [participant.decks_used_today for participant in participants]
    if any(value is None for value in values):
        return None
    return sum(values)


def _official_clan_overviews(snapshot):
    """Adapt normalized RaceClan values to the existing dashboard contract."""

    race = snapshot.race
    direct = snapshot.clan
    clans = []
    if race is not None:
        for race_clan in race.clans:
            name = race_clan.name
            if not name and direct is not None and race_clan.clan_tag == direct.clan_tag:
                name = direct.name
            if not name:
                continue

            decks_used = race_clan.decks_used_today
            if decks_used is None:
                decks_used = _known_decks_used(race_clan.participants)

            average = None
            if race_clan.fame is not None and decks_used not in (None, 0):
                average = round(race_clan.fame / decks_used, 2)

            trophies = race_clan.clan_war_trophies
            if (
                trophies is None
                and direct is not None
                and race_clan.clan_tag == direct.clan_tag
            ):
                trophies = direct.clan_war_trophies

            clans.append(
                ClanOverview(
                    name=name,
                    decks_used_today=decks_used,
                    # currentriverrace does not expose a trustworthy total
                    # capacity field in the normalized T02 contract.
                    decks_total_today=None,
                    avg_medals_per_deck=average,
                    # A projected score is not an official field.  Keep it
                    # absent so strategy_engine can label any estimate.
                    projected_medals=None,
                    # repairPoints is the official team-level progress value
                    # closest to the former Boat column; it is labelled in
                    # data_quality below and never replaces fame.
                    boat_points=race_clan.repair_points,
                    current_medals=race_clan.fame,
                    trophies=trophies,
                )
            )

    if not clans and direct is not None and direct.name:
        # A direct clan response identifies the selected clan, but it is not a
        # current-race performance snapshot.  Keep only identity/trophy data.
        clans.append(
            ClanOverview(
                name=direct.name,
                decks_used_today=None,
                decks_total_today=None,
                avg_medals_per_deck=None,
                projected_medals=None,
                boat_points=None,
                current_medals=None,
                trophies=direct.clan_war_trophies,
            )
        )
    return clans


def _legacy_player_row(
    profile: PlayerProfile | None,
    participant: RaceParticipant | None,
    rank,
    *,
    race_status,
):
    """Create one legacy player row without defaulting missing metrics to 0."""

    name = (
        participant.name
        if participant is not None and participant.name
        else profile.name
        if profile is not None
        else ""
    )
    role = (
        participant.role
        if participant is not None and participant.role
        else profile.role
        if profile is not None and profile.role
        else ""
    )
    fetched_at = (
        participant.fetched_at
        if participant is not None
        else profile.fetched_at
        if profile is not None
        else None
    )
    stale = bool(
        participant.is_stale
        if participant is not None
        else profile.is_stale
        if profile is not None
        else False
    )

    row = {
        "rank": rank,
        "tag": (
            participant.player_tag
            if participant is not None and participant.player_tag
            else profile.player_tag
            if profile is not None
            else ""
        ),
        "name": name,
        "role": role,
        "decks_used_today": participant.decks_used_today if participant is not None else None,
        "decks_total_so_far": participant.decks_used if participant is not None else None,
        "boat_attacks": (
            participant.boat_attacks_today
            if participant is not None and participant.boat_attacks_today is not None
            else participant.boat_attacks
            if participant is not None
            else None
        ),
        "fame": participant.fame if participant is not None else None,
        "repair_points": participant.repair_points if participant is not None else None,
        "decks_used": participant.decks_used if participant is not None else None,
        "boat_attacks_today": (
            participant.boat_attacks_today if participant is not None else None
        ),
        "boat_attacks_total": participant.boat_attacks if participant is not None else None,
        "boat_defenses_today": (
            participant.boat_defenses_today if participant is not None else None
        ),
        "boat_defenses_total": participant.boat_defenses if participant is not None else None,
        "trophies": profile.trophies if profile is not None else None,
        "best_trophies": profile.best_trophies if profile is not None else None,
        "source": OFFICIAL_SOURCE,
        "fetched_at": _safe_timestamp(fetched_at),
        "data_status": (
            participant.data_status
            if participant is not None
            else DATA_STATUS_PARTIAL
            if race_status not in {DATA_STATUS_EMPTY, DATA_STATUS_ERROR}
            else race_status
        ),
        "is_stale": stale,
        "stale": stale,
        "stale_reason": (
            participant.stale_reason
            if participant is not None
            else profile.stale_reason
            if profile is not None
            else None
        ),
    }
    if (
        participant is not None
        and participant.fame is not None
        and participant.repair_points is not None
    ):
        row["contribution"] = participant.fame + participant.repair_points
    else:
        row["contribution"] = None
    return row


def _official_player_rows(snapshot, race_status):
    """Join normalized members to normalized participants by canonical tag."""

    participants = snapshot.race.participants if snapshot.race is not None else ()
    by_tag = {
        participant.player_tag: participant
        for participant in participants
        if participant.player_tag
    }
    rows = []
    seen = set()

    for profile in snapshot.members:
        participant = by_tag.pop(profile.player_tag, None)
        if profile.player_tag:
            seen.add(profile.player_tag)
        rows.append(
            _legacy_player_row(
                profile,
                participant,
                len(rows) + 1,
                race_status=race_status,
            )
        )

    for participant in participants:
        if not participant.player_tag or participant.player_tag in seen:
            continue
        rows.append(
            _legacy_player_row(
                None,
                participant,
                len(rows) + 1,
                race_status=race_status,
            )
        )
    return rows


def _players_have_complete_deck_usage(players):
    """Return whether every displayed member has an official deck count."""

    return bool(players) and all(
        row.get("decks_used_today") is not None for row in players
    )


def _render_incomplete_stats(status):
    message = _status_text(status)
    return (
        "Clan Stats:\n"
        f"- Data status: {status}\n"
        f"- Battles left: onbekend ({message})\n"
        f"- Duels left: onbekend ({message})\n"
        "- Total players participated: onbekend"
    )


def _official_active_day(snapshot):
    if snapshot.race is None:
        return None
    context: RaceContext = snapshot.race.context
    state = str(context.state or "").strip().lower().replace("_", "")
    if state in {"notinwar", "inactive", "finished", "warended"}:
        return None
    for value in (context.period_index, context.section_index):
        if value in {1, 2, 3, 4}:
            return value
    return None


def _official_colosseum(snapshot):
    if snapshot.race is None:
        return False
    context = snapshot.race.context
    period = str(context.period_type or "").strip().lower()
    if "colosseum" in period:
        return True
    payload = _response_payload(snapshot.race_response)
    if not isinstance(payload, Mapping):
        return False
    for key in ("isColosseumWeekend", "is_colosseum_weekend", "colosseum"):
        value = payload.get(key)
        if isinstance(value, bool):
            return value
    return False


def _official_race_started(snapshot):
    if snapshot.race is None:
        return False
    state = str(snapshot.race.context.state or "").strip().lower().replace("_", "")
    if state in {"notinwar", "inactive", "finished", "warended"}:
        return False
    return bool(state or snapshot.race.clans or snapshot.race.participants)


def _explicit_empty_race(snapshot):
    if snapshot.race_response is None or snapshot.race is None:
        return False
    if _response_status(snapshot.race_response) == DATA_STATUS_EMPTY:
        return True
    state = str(snapshot.race.context.state or "").strip().lower().replace("_", "")
    if state in {"notinwar", "inactive", "finished", "warended"}:
        return True
    return (
        not snapshot.race.clans
        and not snapshot.race.participants
        and not state
    )


def _snapshot_data_status(snapshot):
    endpoint_failures = set(snapshot.endpoint_errors) | set(snapshot.normalization_errors)
    if len(endpoint_failures) == 3:
        return DATA_STATUS_ERROR
    if _explicit_empty_race(snapshot) and not endpoint_failures:
        return DATA_STATUS_EMPTY
    if endpoint_failures:
        return DATA_STATUS_PARTIAL
    if snapshot.race is None:
        return DATA_STATUS_PARTIAL
    if snapshot.race.data_status in {DATA_STATUS_ERROR, DATA_STATUS_INVALID, DATA_STATUS_PARTIAL}:
        return DATA_STATUS_PARTIAL
    if snapshot.race.data_status == DATA_STATUS_EMPTY:
        return DATA_STATUS_EMPTY
    if not snapshot.race.clans:
        return DATA_STATUS_PARTIAL
    if not snapshot.members:
        return DATA_STATUS_PARTIAL
    if any(
        _response_stale(response)
        for response in (
            snapshot.clan_response,
            snapshot.members_response,
            snapshot.race_response,
        )
        if response is not None
    ) or snapshot.race.is_stale:
        return DATA_STATUS_STALE
    return DATA_STATUS_FRESH


def _endpoint_records(snapshot, clan_tag):
    records = []
    for name, response in (
        ("clan", snapshot.clan_response),
        ("members", snapshot.members_response),
        ("race", snapshot.race_response),
    ):
        error = snapshot.normalization_errors.get(name) or snapshot.endpoint_errors.get(name)
        records.append(_endpoint_record(name, clan_tag, response, error=error))
    return records


def _snapshot_fetched_at(records):
    values = [record.get("fetched_at") for record in records if record.get("fetched_at")]
    if values:
        return max(values)
    return datetime.now(timezone.utc).isoformat()


def _combined_source(fallback_used=False):
    return (
        f"{OFFICIAL_SOURCE}+{HTML_FALLBACK_SOURCE}"
        if fallback_used
        else OFFICIAL_SOURCE
    )


def _fallback_enabled(value):
    if value is not None:
        return bool(value)
    return os.environ.get(HTML_FALLBACK_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _load_html_fallback(clan_config):
    """Load the old cwstats parser only as an explicit temporary fallback."""

    fallback = _HtmlFallback()
    cwstats_url = f"https://cwstats.com/clan/{clan_config.get('tag')}/race"
    try:
        html = fetch_html(cwstats_url)
    except Exception:
        return fallback

    fallback.finish_outlook = parse_cwstats_finish_outlook_from_html(html)
    fallback.race_context = parse_cwstats_race_context_from_html(html)
    fallback.players = parse_cwstats_players_from_html(html)
    for row in (fallback.race_context.get("rows_by_name") or {}).values():
        fallback.clans.append(
            ClanOverview(
                name=str(row.get("name") or ""),
                decks_used_today=None,
                decks_total_today=None,
                avg_medals_per_deck=_safe_float(row.get("fame_avg")),
                projected_medals=None,
                boat_points=_safe_int(row.get("boat_movement")),
                current_medals=(
                    _safe_int(row.get("fame"))
                    if row.get("fame") is not None
                    else _safe_int(row.get("trophy"))
                ),
                trophies=(
                    _safe_int(row.get("clan_war_trophies"))
                    if row.get("clan_war_trophies") is not None
                    else _safe_int(row.get("cw_trophy"))
                ),
            )
        )
    for row in fallback.players:
        row["source"] = HTML_FALLBACK_SOURCE
        row["data_status"] = DATA_STATUS_PARTIAL
        row["is_stale"] = False
        row["stale"] = False
        row["fetched_at"] = None
    return fallback


def _merge_fallback_clans(official_clans, fallback_clans):
    if not fallback_clans:
        return list(official_clans)
    merged = list(official_clans)
    official_names = {_normalize_clan_name(clan.name) for clan in merged}
    for clan in fallback_clans:
        key = _normalize_clan_name(clan.name)
        if key and key not in official_names:
            merged.append(clan)
            official_names.add(key)
    return merged


def _merge_fallback_players(official_players, fallback_players):
    if not fallback_players:
        return list(official_players)
    merged = list(official_players)
    existing_tags = {
        (row.get("tag") or "").strip().upper()
        for row in merged
        if row.get("tag")
    }
    existing_names = {
        (row.get("name") or "").strip().upper()
        for row in merged
        if row.get("name")
    }
    for row in fallback_players:
        tag_key = (row.get("tag") or "").strip().upper()
        name_key = (row.get("name") or "").strip().upper()
        if (tag_key and tag_key in existing_tags) or (
            name_key and name_key in existing_names
        ):
            continue
        copied = dict(row)
        copied["rank"] = len(merged) + 1
        merged.append(copied)
        if tag_key:
            existing_tags.add(tag_key)
        if name_key:
            existing_names.add(name_key)
    return merged


def _quality(snapshot, records, status, clans, players, *, fallback_used):
    missing = set()
    estimated = set()
    errors = []

    for record in records:
        if record.get("data_status") == DATA_STATUS_ERROR:
            errors.append(
                {
                    "endpoint": record.get("endpoint"),
                    "code": record.get("error_code") or "endpoint_error",
                    "status_code": record.get("status_code"),
                }
            )

    if snapshot.clan is None or not snapshot.clan.name:
        missing.add("clan")
    if not snapshot.members:
        missing.add("members")
    if snapshot.race is None:
        missing.add("race")
    elif not snapshot.race.clans:
        missing.add("race.clans")

    if not players and status not in {DATA_STATUS_EMPTY, DATA_STATUS_ERROR}:
        missing.add("race.participants")
    if any(row.get("decks_used_today") is None for row in players):
        # A member without a participant is not a zero-performance row.  The
        # visible race snapshot cannot establish the member's remaining decks.
        missing.add("participantStatus")
    if clans and any(clan.current_medals is None for clan in clans):
        missing.add("currentMedals")
    if clans and any(clan.projected_medals is None for clan in clans):
        missing.add("projectedMedals")
    if any(clan.avg_medals_per_deck is not None for clan in clans):
        estimated.add("avg_medals_per_deck")
    if any(clan.boat_points is not None for clan in clans):
        estimated.add("boat_points_from_repairPoints")

    if status in {DATA_STATUS_FRESH, DATA_STATUS_STALE} and not missing:
        confidence = "high" if status == DATA_STATUS_FRESH else "medium"
    elif status == DATA_STATUS_EMPTY:
        confidence = "low"
    else:
        confidence = "medium" if clans or players else "low"

    sources = [OFFICIAL_SOURCE]
    if fallback_used:
        sources.append(HTML_FALLBACK_SOURCE)
    return {
        "status": status,
        "dataStatus": status,
        "confidence": confidence,
        "missingFields": sorted(missing),
        "missing_fields": sorted(missing),
        "estimatedFields": sorted(estimated),
        "estimated_fields": sorted(estimated),
        "sources": sources,
        "normalizers": [
            "RaceContext",
            "RaceClan",
            "RaceParticipant",
            "PlayerProfile",
        ],
        "errors": errors,
        "endpoints": records,
        "endpointMetadata": records,
        "fallback": {
            "used": bool(fallback_used),
            "temporary": True,
            "official_data_precedence": True,
            "source": HTML_FALLBACK_SOURCE if fallback_used else None,
        },
        "metricSources": {
            "current_medals": "official currentriverrace clan.fame",
            "boat_points": "official currentriverrace clan.repairPoints",
            "avg_medals_per_deck": "derived from official fame/decksUsedToday",
            "projected_medals": "not supplied by currentriverrace; left unknown",
        },
    }


def _status_text(status):
    return {
        DATA_STATUS_EMPTY: "Geen actieve officiële current river race beschikbaar.",
        DATA_STATUS_ERROR: "Officiële Clash Royale-data is tijdelijk niet beschikbaar.",
        DATA_STATUS_PARTIAL: "Officiële Clash Royale-data is niet volledig beschikbaar.",
        DATA_STATUS_STALE: "Officiële data is beschikbaar uit een stale snapshot.",
    }.get(status, "Officiële data beschikbaar.")


def _safe_error_payload(error):
    """Return a route error without exposing exception text or upstream bodies."""

    code = _safe_code(getattr(error, "code", "server_error"), "server_error")
    now = datetime.now(timezone.utc).isoformat()
    return {
        "ok": False,
        "http_ok": False,
        "http_status": 500,
        "source": OFFICIAL_SOURCE,
        "fetched_at": now,
        "data_status": DATA_STATUS_ERROR,
        "error": "Officiële Clash Royale-data kon niet worden opgehaald.",
        "data_quality": {
            "status": DATA_STATUS_ERROR,
            "confidence": "low",
            "missingFields": ["clan", "members", "race"],
            "estimatedFields": [],
            "sources": [OFFICIAL_SOURCE],
            "errors": [{"code": code}],
        },
        "metadata": {
            "source": OFFICIAL_SOURCE,
            "fetched_at": now,
            "data_status": DATA_STATUS_ERROR,
            "is_stale": False,
            "stale": False,
            "stale_reason": None,
            "error_code": code,
            "endpoints": [],
        },
        "endpoint_metadata": [],
    }


def build_cwstats_payload(path, client=None, *, allow_html_fallback=None):
    """Build the backwards-compatible cwstats JSON payload.

    ``client`` is intentionally injectable for deterministic route tests.  In
    production the default ``ClashRoyaleClient`` reads the API key server-side.
    """

    clan_config = pick_clan_config(path)
    clan_tag = normalize_tag(clan_config.get("tag") or "")
    snapshot = fetch_official_snapshot(clan_tag, client=client)
    status = _snapshot_data_status(snapshot)
    fallback = _HtmlFallback()
    fallback_used = False

    # A normal empty race is an explicit business state, not a reason to show
    # an older scraped race.  Fallback is reserved for actual official gaps.
    if _fallback_enabled(allow_html_fallback) and (
        snapshot.endpoint_errors or snapshot.normalization_errors
    ):
        fallback = _load_html_fallback(clan_config)
        fallback_used = bool(
            any(value is not None for value in fallback.finish_outlook.values())
            or fallback.race_context.get("active_day") in {1, 2, 3, 4}
            or fallback.race_context.get("rows_by_name")
            or fallback.players
            or fallback.clans
        )

    clans = [] if status == DATA_STATUS_EMPTY else _official_clan_overviews(snapshot)
    players = _official_player_rows(snapshot, status)
    if fallback_used:
        clans = _merge_fallback_clans(clans, fallback.clans)
        players = _merge_fallback_players(players, fallback.players)

    official_day = _official_active_day(snapshot)
    fallback_day = fallback.race_context.get("active_day")
    active_day = official_day if official_day in {1, 2, 3, 4} else fallback_day
    official_colosseum = _official_colosseum(snapshot)
    is_colosseum = official_colosseum or bool(
        fallback.race_context.get("is_colosseum_weekend") if fallback_used else False
    )
    war_phase = build_war_phase(
        active_day,
        {
            "active_day": active_day,
            "is_colosseum_weekend": is_colosseum,
        },
    )
    if official_day is None and fallback_day in {1, 2, 3, 4}:
        war_phase["source"] = HTML_FALLBACK_SOURCE
        war_phase["confidence"] = "low"

    response_records = _endpoint_records(snapshot, clan_tag)
    fetched_at = _snapshot_fetched_at(response_records)
    stale = any(record.get("is_stale") for record in response_records)
    stale_reason = next(
        (
            record.get("stale_reason")
            for record in response_records
            if record.get("stale_reason")
        ),
        None,
    )
    error_code = next(
        (
            record.get("error_code")
            for record in response_records
            if record.get("error_code")
        ),
        None,
    )
    source = _combined_source(fallback_used)

    clan_name = clan_config.get("name") or OUR_CLAN_NAME_DEFAULT
    if snapshot.clan is not None and snapshot.clan.name:
        clan_name = snapshot.clan.name if clan_tag == snapshot.clan.clan_tag else clan_name

    finish_outlook = {}
    if fallback_used and fallback.finish_outlook:
        # Omit unavailable fallback numbers instead of serializing nulls;
        # JavaScript's Number(null) would otherwise look like a real zero.
        finish_outlook = {
            key: value
            for key, value in fallback.finish_outlook.items()
            if value is not None
        }
        finish_outlook.update(
            {
                "source": HTML_FALLBACK_SOURCE,
                "data_status": DATA_STATUS_PARTIAL,
                "model": "temporary_html_fallback",
            }
        )
    else:
        finish_outlook = {
            "source": source,
            "data_status": status,
            "model": "official_api_no_finish_projection",
        }

    # Strategy is only fed race data when the normalized race is usable.  Its
    # own dataQuality output still records estimates such as theoretical deck
    # capacity; T03 does not alter the strategy engine.
    strategy_input_clans = clans if status in {DATA_STATUS_FRESH, DATA_STATUS_STALE} else []
    strategy_input_players = players if status in {DATA_STATUS_FRESH, DATA_STATUS_STALE} else []
    strategy_package = strategy_engine.build_strategy_package(
        strategy_input_clans,
        clan_name,
        strategy_input_players,
        finish_outlook if fallback_used else {},
        war_phase,
    )
    quality = _quality(
        snapshot,
        response_records,
        status,
        clans,
        players,
        fallback_used=fallback_used,
    )
    strategy_quality = strategy_package.get("dataQuality") or {}
    quality["missingFields"] = sorted(
        set(quality["missingFields"]) | set(strategy_quality.get("missingFields") or [])
    )
    quality["missing_fields"] = quality["missingFields"]
    quality["estimatedFields"] = sorted(
        set(quality["estimatedFields"])
        | set(strategy_quality.get("estimatedFields") or [])
    )
    quality["estimated_fields"] = quality["estimatedFields"]

    race_rows = strategy_package.get("raceRows") or []
    if not race_rows and clans:
        # Keep the official normalized race visible even when strategy is
        # intentionally disabled for a partial/error snapshot.
        race_rows = build_race_rows(clans)
        for row in race_rows:
            row.update(
                {
                    "currentMedals": row.get("medals"),
                    "decksUsedToday": (row.get("decks") or {}).get("used"),
                    "estimatedDeckCapacityToday": (row.get("decks") or {}).get("total"),
                    "decksRemainingToday": (row.get("decks") or {}).get("remaining"),
                    "todayAveragePerDeck": row.get("avg"),
                    "boatValueRaw": row.get("boat_points"),
                }
            )
    for row in race_rows:
        row["source"] = source
        row["fetched_at"] = fetched_at
        row["data_status"] = status
        row["is_stale"] = stale
        row["stale"] = stale

    report_context = _OfficialReportContext(active_day)
    render_clans = _copy_clan_overviews(clans)
    race_overview_text = (
        render_clan_overview_table(render_clans)
        if render_clans
        else f"Clan overview: {_status_text(status)}"
    )
    insights_text = render_clan_insights(
        render_clans,
        clan_name,
    )
    clan_avg_projection_text = render_clan_avg_projection(render_clans)
    complete_player_decks = _players_have_complete_deck_usage(players)
    if status in {DATA_STATUS_FRESH, DATA_STATUS_STALE} and complete_player_decks:
        clan_stats_text = render_clan_stats_block(
            report_context,
            render_clans,
            clan_name,
            players,
        )
        battles_left_text = render_battles_left_today(players)
        risk_left_text = render_risk_left_attacks(players)
        high_fame_text = render_high_fame_players(report_context, players)
        day1_high_famers = collect_day1_high_famers(report_context, players)
        day1_high_fame_text = render_day1_high_fame_players(report_context, players)
        day4_last_chance_text = render_day4_last_chance_players(report_context, players)
        short_story_text = build_short_story(
            report_context,
            render_clans,
            clan_name,
            players,
            max_chars=220,
        )
    elif status in {DATA_STATUS_FRESH, DATA_STATUS_STALE}:
        clan_stats_text = _render_incomplete_stats(status)
        battles_left_text = (
            "Battles left (today):\n\n"
            "Onbekend: niet iedere member heeft een zichtbare race-participant."
        )
        risk_left_text = (
            "Spelers met nog losse aanvallen:\n\n"
            "Onbekend: participant-status is niet volledig zichtbaar."
        )
        high_fame_text = ""
        day1_high_famers = []
        day1_high_fame_text = ""
        day4_last_chance_text = ""
        short_story_text = f"War update: {_status_text(status)}"
    else:
        clan_stats_text = (
            "Clan Stats:\n"
            f"- Data status: {status}\n"
            "- Officiële race-/bijdragecijfers blijven onbekend zolang de race niet volledig beschikbaar is."
        )
        battles_left_text = f"Battles left (today):\n\n{_status_text(status)}"
        risk_left_text = f"Spelers met nog losse aanvallen:\n\n{_status_text(status)}"
        high_fame_text = ""
        day1_high_famers = []
        day1_high_fame_text = ""
        day4_last_chance_text = ""
        short_story_text = f"War update: {_status_text(status)}"

    total_players_participated = (
        compute_total_players_participated(players)
        if status in {DATA_STATUS_FRESH, DATA_STATUS_STALE}
        and complete_player_decks
        else None
    )
    players_text = (
        render_player_table(players)
        if players
        else f"Players: {_status_text(status)}"
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
        ("Strategieadvies", (strategy_package.get("recommendation") or {}).get("summary")),
    ]
    copy_all_parts = []
    for title, text in sections:
        if not text:
            continue
        copy_all_parts.extend((title, text))

    endpoint_errors = bool(snapshot.endpoint_errors or snapshot.normalization_errors)
    ok = not endpoint_errors
    payload = {
        "ok": ok,
        "http_ok": True,
        "http_status": 200,
        "error": None if ok else "Officiële data is niet volledig beschikbaar.",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "fetched_at": fetched_at,
        "data_status": status,
        "data_quality": quality,
        "metadata": {
            "source": source,
            "fetched_at": fetched_at,
            "data_status": status,
            "is_stale": stale,
            "stale": stale,
            "stale_reason": stale_reason,
            "error_code": error_code,
            "endpoints": response_records,
        },
        "endpoint_metadata": response_records,
        # Existing frontend response fields remain unchanged below.
        "race_overview_text": race_overview_text,
        "insights_text": insights_text,
        "clan_stats_text": clan_stats_text,
        "clan_avg_projection_text": clan_avg_projection_text,
        "players_text": players_text,
        "battles_left_text": battles_left_text,
        "risk_left_text": risk_left_text,
        "high_fame_text": high_fame_text,
        "day1_high_famers": [
            {"name": name, "fame": fame} for name, fame in day1_high_famers
        ],
        "day1_high_fame_text": day1_high_fame_text,
        "day4_last_chance_text": day4_last_chance_text,
        "short_story_text": short_story_text,
        "short_story_limit": 220,
        "clan_tag": clan_tag,
        "clan_name": clan_name,
        "copy_all_text": "\n\n".join(copy_all_parts),
        "finish_outlook": finish_outlook,
        "war_phase": war_phase,
        "war_context": strategy_package.get("warContext"),
        "race_rows": race_rows,
        "strategy": strategy_package.get("recommendation") or {},
        "rank_targets": strategy_package.get("rankTargets") or [],
        "projections": strategy_package.get("projections") or [],
        "cwstats_colosseum_weekend": bool(is_colosseum),
        "cwstats_active_day": war_phase.get("day"),
        "total_players_participated": total_players_participated,
        "clan_access_type": (
            "Open"
            if snapshot.clan is not None
            and str(snapshot.clan.clan_type or "").lower() == "open"
            else "Invite Only"
            if snapshot.clan is not None
            and str(snapshot.clan.clan_type or "").lower() in {"inviteonly", "invite_only"}
            else snapshot.clan.clan_type
            if snapshot.clan is not None
            else None
        ),
        "cw_official_started": _official_race_started(snapshot),
        "warnings": [
            *[
                f"Official endpoint {record['name']} unavailable ({record.get('error_code') or 'endpoint_error'})."
                for record in response_records
                if record.get("data_status") == DATA_STATUS_ERROR
            ],
            *([f"{_status_text(status)}"] if status in {DATA_STATUS_EMPTY, DATA_STATUS_PARTIAL, DATA_STATUS_ERROR, DATA_STATUS_STALE} else []),
            *(["Tijdelijke HTML-fallback gebruikt; officiële data bleef leidend."] if fallback_used else []),
        ],
    }
    return payload


def pick_clan_config(path: str):
    parsed = urlparse(path)
    params = parse_qs(parsed.query)
    return get_clan_config(params.get("clan", [""])[0])


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            payload = build_cwstats_payload(self.path)
            self._send_json(200, payload)
        except Exception as error:
            payload = _safe_error_payload(error)
            self._send_json(500, payload)

    def _send_json(self, status_code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
