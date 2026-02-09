import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from Royale_api import normalize_tag, fetch_clan_members

LEADERBOARD_URL_DEFAULT = "https://royaleapi.com/clans/war/nl"
WAR_LOG_URL_TEMPLATE = "https://royaleapi.com/clan/{tag}/war/log"

API_BASES = [
    "https://api.royaleapi.com",
    "https://royaleapi.com/api",
]

SNAPSHOT_PATH = os.environ.get("RECAP_SNAPSHOT_PATH", "/tmp/recap_snapshots.json")


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip())


def build_headers() -> Dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "nl,en-US;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Connection": "close",
    }


def fetch_html(url: str, timeout: int = 25) -> str:
    response = requests.get(url, headers=build_headers(), timeout=timeout)
    if response.status_code >= 400:
        raise RuntimeError(f"HTTP {response.status_code} bij ophalen van {url}")
    return response.text


def fetch_api_json(urls: List[str], token: str, timeout: int = 25) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    errors: List[str] = []
    headers = {
        "User-Agent": build_headers()["User-Agent"],
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    for url in urls:
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            if response.status_code != 200:
                errors.append(f"API HTTP {response.status_code} bij {url}")
                continue
            return response.json(), errors
        except Exception as exc:
            errors.append(f"API error bij {url}: {exc}")
    return None, errors


def load_snapshots() -> Dict[str, List[Dict[str, Any]]]:
    try:
        with open(SNAPSHOT_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            if isinstance(data, dict):
                return data
    except FileNotFoundError:
        return {}
    except Exception:
        return {}
    return {}


def save_snapshots(data: Dict[str, List[Dict[str, Any]]]) -> None:
    os.makedirs(os.path.dirname(SNAPSHOT_PATH), exist_ok=True)
    with open(SNAPSHOT_PATH, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def store_snapshot(clan_tag: str, rank: int, trophies: int) -> Dict[str, Any]:
    data = load_snapshots()
    entries = data.get(clan_tag, [])
    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "rank": rank,
        "trophies": trophies,
    }
    entries.append(payload)
    data[clan_tag] = entries
    save_snapshots(data)
    return payload


def get_previous_snapshot(clan_tag: str) -> Optional[Dict[str, Any]]:
    data = load_snapshots()
    entries = data.get(clan_tag, [])
    if not entries:
        return None
    return entries[-1]


def compute_movement(rank_now: Optional[int], rank_prev: Optional[int]) -> str:
    if rank_now is None or rank_prev is None:
        return "unknown"
    if rank_now < rank_prev:
        return "gestegen"
    if rank_now > rank_prev:
        return "gezakt"
    return "gelijk"


def parse_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = normalize_space(str(value))
    if not text:
        return None
    match = re.search(r"-?\d+", text.replace(",", ""))
    return int(match.group(0)) if match else None


def parse_leaderboard_json(data: Dict[str, Any], clan_tag: str) -> Optional[Dict[str, Any]]:
    normalized = normalize_tag(clan_tag)
    entries = None
    for key in ["items", "clans", "data", "entries"]:
        if isinstance(data.get(key), list):
            entries = data.get(key)
            break
    if entries is None and isinstance(data.get("items"), dict):
        entries = data.get("items").get("items")
    if not entries:
        return None

    for entry in entries:
        tag = entry.get("tag") or entry.get("clan", {}).get("tag")
        if not tag:
            continue
        if normalize_tag(tag) != normalized:
            continue
        rank = parse_int(entry.get("rank") or entry.get("position"))
        trophies = parse_int(
            entry.get("trophies")
            or entry.get("warTrophies")
            or entry.get("trophiesAfter")
        )
        return {"rank": rank, "trophies": trophies}
    return None


def parse_leaderboard_html(html: str, clan_tag: str, clan_name: str) -> Optional[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    normalized_tag = normalize_tag(clan_tag)
    clan_name_clean = normalize_space(clan_name).lower()

    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        row_text = normalize_space(tr.get_text(" ", strip=True))
        row_text_lower = row_text.lower()
        tag_hit = normalized_tag and normalized_tag in row_text.replace("#", "").upper()
        name_hit = clan_name_clean and clan_name_clean in row_text_lower
        if not (tag_hit or name_hit):
            continue

        texts = [normalize_space(c.get_text(" ", strip=True)) for c in cells]
        rank = None
        for cell in texts:
            if cell.isdigit():
                rank = int(cell)
                break
        trophies = None
        for cell in reversed(texts):
            if not cell or "/" in cell:
                continue
            if re.fullmatch(r"-?\d+", cell.replace(",", "")):
                trophies = int(cell.replace(",", ""))
                break
        return {"rank": rank, "trophies": trophies}
    return None


def collect_recap_data(clan_tag: str, clan_name: str, leaderboard_url: str = LEADERBOARD_URL_DEFAULT) -> Dict[str, Any]:
    errors: List[str] = []
    data = None
    source = "scrape"
    token = os.environ.get("ROYALEAPI_TOKEN")
    if token:
        api_urls = [f"{base}/clans/war/nl" for base in API_BASES]
        api_data, api_errors = fetch_api_json(api_urls, token)
        errors.extend(api_errors)
        if api_data:
            parsed = parse_leaderboard_json(api_data, clan_tag)
            if parsed:
                data = parsed
                source = "api"
            else:
                errors.append("API parsing mismatch voor leaderboard.")
    if data is None:
        try:
            html = fetch_html(leaderboard_url)
            parsed = parse_leaderboard_html(html, clan_tag, clan_name)
            if parsed:
                data = parsed
            else:
                errors.append("HTML parsing mismatch voor leaderboard.")
        except Exception as exc:
            errors.append(str(exc))

    rank_now = data.get("rank") if data else None
    trophies_now = data.get("trophies") if data else None

    previous = get_previous_snapshot(clan_tag)
    rank_prev = previous.get("rank") if previous else None

    movement = compute_movement(rank_now, rank_prev)
    snapshot = None
    if rank_now is not None and trophies_now is not None:
        snapshot = store_snapshot(clan_tag, rank_now, trophies_now)

    return {
        "rank_now": rank_now,
        "rank_prev": rank_prev,
        "trophies_now": trophies_now,
        "movement": movement,
        "snapshot": snapshot,
        "source": source,
        "errors": errors,
    }


def find_latest_entry(entries: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    def key_fn(item: Dict[str, Any]) -> Tuple[int, int]:
        season = parse_int(
            item.get("seasonId")
            or item.get("season")
            or item.get("season_id")
            or item.get("seasonNumber")
        )
        week = parse_int(
            item.get("week")
            or item.get("weekNumber")
            or item.get("sectionIndex")
            or item.get("section_index")
        )
        return (season or 0, week or 0)

    if not entries:
        return None
    return max(entries, key=key_fn)


def parse_warlog_json(data: Dict[str, Any], clan_tag: str) -> Optional[Dict[str, Any]]:
    normalized = normalize_tag(clan_tag)
    entries = None
    for key in ["items", "warlog", "data", "itemsList"]:
        if isinstance(data.get(key), list):
            entries = data.get(key)
            break
    if entries is None and isinstance(data.get("items"), dict):
        entries = data.get("items").get("items")
    if not entries:
        return None

    entry = find_latest_entry(entries)
    if not entry:
        return None

    standings = None
    for key in ["standings", "clans", "results", "items"]:
        if isinstance(entry.get(key), list):
            standings = entry.get(key)
            break

    clan_result = None
    if standings:
        for item in standings:
            tag = item.get("tag") or item.get("clan", {}).get("tag")
            if not tag:
                continue
            if normalize_tag(tag) == normalized:
                clan_result = item
                break

    participants = None
    for key in ["participants", "members", "playerResults", "players"]:
        if isinstance(entry.get(key), list):
            participants = entry.get(key)
            break

    return {
        "entry": entry,
        "clan_result": clan_result,
        "participants": participants or [],
    }


def find_table_headers(table: BeautifulSoup) -> List[str]:
    thead = table.find("thead")
    if thead:
        return [normalize_space(th.get_text(" ", strip=True)) for th in thead.find_all("th")]
    first_row = table.find("tr")
    if first_row:
        return [normalize_space(cell.get_text(" ", strip=True)) for cell in first_row.find_all(["th", "td"])]
    return []


def parse_week_summary_table(table: BeautifulSoup) -> List[Dict[str, Any]]:
    headers = find_table_headers(table)
    header_lower = [h.lower() for h in headers]
    rows = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue
        texts = [normalize_space(c.get_text(" ", strip=True)) for c in cells]
        if not texts:
            continue
        if not texts[0].lower().startswith("w"):
            continue
        week = parse_int(texts[0])
        rank = None
        boat_points = None
        trophy_change = None
        trophies_after = None
        for idx, header in enumerate(header_lower):
            value = texts[idx] if idx < len(texts) else ""
            if "rank" in header:
                rank = parse_int(value)
            if "boat" in header:
                boat_points = parse_int(value)
            if "trophy" in header:
                match = re.search(r"([+-]\d+)\s*(\d+)", value)
                if match:
                    trophy_change = int(match.group(1))
                    trophies_after = int(match.group(2))
        rows.append(
            {
                "week": week,
                "rank": rank,
                "boat_points": boat_points,
                "trophy_change": trophy_change,
                "trophies_after": trophies_after,
            }
        )
    return rows


def parse_participants_table(table: BeautifulSoup) -> List[Dict[str, Any]]:
    headers = find_table_headers(table)
    header_lower = [h.lower() for h in headers]
    participants = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if not cells:
            continue
        texts = [normalize_space(c.get_text(" ", strip=True)) for c in cells]
        if not texts:
            continue
        name = texts[0]
        attacks = None
        points = None
        for idx, header in enumerate(header_lower):
            value = texts[idx] if idx < len(texts) else ""
            if "attack" in header:
                attacks = parse_int(value)
            if "point" in header or "fame" in header:
                points = parse_int(value)
        participants.append(
            {
                "name": name,
                "attacks": attacks or 0,
                "points": points or 0,
            }
        )
    return participants


def parse_warlog_html(html: str, clan_tag: str, clan_name: str) -> Optional[Dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    normalized = normalize_tag(clan_tag)
    clan_name_clean = normalize_space(clan_name).lower()

    latest_week: Optional[Dict[str, Any]] = None
    for table in soup.find_all("table"):
        headers = find_table_headers(table)
        if not headers:
            continue
        header_lower = [h.lower() for h in headers]
        if "week" in header_lower and any("rank" in h for h in header_lower):
            rows = parse_week_summary_table(table)
            for row in rows:
                if latest_week is None:
                    latest_week = row
                else:
                    if (row.get("week") or 0) > (latest_week.get("week") or 0):
                        latest_week = row

    clan_result = None
    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            row_text = normalize_space(tr.get_text(" ", strip=True))
            if not row_text:
                continue
            if normalized in row_text.replace("#", "").upper() or clan_name_clean in row_text.lower():
                cells = [normalize_space(c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"])]
                rank = None
                boat_points = None
                trophy_change = None
                trophies_after = None
                for cell in cells:
                    if rank is None and cell.isdigit():
                        rank = int(cell)
                for cell in cells:
                    if "+" in cell or "-" in cell:
                        match = re.search(r"([+-]\d+)\s*(\d+)", cell)
                        if match:
                            trophy_change = int(match.group(1))
                            trophies_after = int(match.group(2))
                for cell in cells:
                    if re.fullmatch(r"\d+", cell) and boat_points is None:
                        boat_points = int(cell)
                clan_result = {
                    "rank": rank,
                    "boat_points": boat_points,
                    "trophy_change": trophy_change,
                    "trophies_after": trophies_after,
                }
                break
        if clan_result:
            break

    participants = []
    for table in soup.find_all("table"):
        headers = find_table_headers(table)
        header_lower = [h.lower() for h in headers]
        if any("attack" in h for h in header_lower) and any("point" in h for h in header_lower):
            participants = parse_participants_table(table)
            if participants:
                break

    if latest_week or clan_result or participants:
        return {
            "latest_week": latest_week,
            "clan_result": clan_result,
            "participants": participants,
        }
    return None


def build_end_war_payload(
    clan_tag: str,
    clan_name: str,
    clan_result: Optional[Dict[str, Any]],
    participants: List[Dict[str, Any]],
    errors: List[str],
    source: str,
) -> Dict[str, Any]:
    rank = parse_int(clan_result.get("rank")) if clan_result else None
    boat_points = parse_int(clan_result.get("boat_points")) if clan_result else None
    trophy_change = parse_int(clan_result.get("trophyChange") if clan_result else None)
    trophies_after = parse_int(clan_result.get("trophies") if clan_result else None)

    if clan_result:
        trophy_change = trophy_change if trophy_change is not None else parse_int(clan_result.get("trophy_change"))
        trophies_after = trophies_after if trophies_after is not None else parse_int(clan_result.get("trophies_after"))
        boat_points = boat_points if boat_points is not None else parse_int(clan_result.get("boatPoints"))

    parsed_participants = []
    for item in participants or []:
        name = item.get("name") or item.get("player") or item.get("playerName") or ""
        attacks = parse_int(item.get("attacks")) or 0
        points = parse_int(item.get("points") or item.get("fame") or item.get("score")) or 0
        tag = item.get("tag") or item.get("playerTag") or item.get("id")
        parsed_participants.append(
            {
                "name": name,
                "attacks": attacks,
                "points": points,
                "tag": normalize_tag(tag) if tag else None,
            }
        )

    top_player = None
    if parsed_participants:
        top_player = max(parsed_participants, key=lambda p: p.get("points", 0))

    full_attackers = [p for p in parsed_participants if p.get("attacks") == 16]
    sum_points_16 = sum(p.get("points", 0) for p in full_attackers)

    missers = [p for p in parsed_participants if 0 < p.get("attacks", 0) < 16]
    missed_attacks_total = sum(16 - p.get("attacks", 0) for p in missers)

    filtered_missers = missers
    member_filter_applied = False
    try:
        tags, names = fetch_clan_members(f"https://royaleapi.com/clan/{normalize_tag(clan_tag)}")
        if tags or names:
            member_filter_applied = True
            normalized_names = {normalize_space(n).lower() for n in names}
            filtered_missers = [
                p
                for p in missers
                if (p.get("tag") and p.get("tag") in tags)
                or normalize_space(p.get("name", "")).lower() in normalized_names
            ]
    except Exception as exc:
        errors.append(f"Clan member filter faalde: {exc}")

    missers_payload = [
        {
            "name": p.get("name"),
            "attacks": p.get("attacks"),
            "missed": 16 - p.get("attacks", 0),
            "points": p.get("points"),
        }
        for p in filtered_missers
    ]

    return {
        "rank": rank,
        "boat_points": boat_points,
        "trophy_change": trophy_change,
        "trophies_after": trophies_after,
        "top_player": top_player,
        "count16": len(full_attackers),
        "sum_points_16": sum_points_16,
        "missed_attacks_total": missed_attacks_total,
        "missers": missers_payload,
        "member_filter_applied": member_filter_applied,
        "source": source,
        "errors": errors,
        "clan_tag": clan_tag,
        "clan_name": clan_name,
    }


def collect_end_war_data(clan_tag: str, clan_name: str) -> Dict[str, Any]:
    errors: List[str] = []
    source = "scrape"
    clan_result = None
    participants: List[Dict[str, Any]] = []

    token = os.environ.get("ROYALEAPI_TOKEN")
    if token:
        api_urls = [f"{base}/clan/{normalize_tag(clan_tag)}/warlog" for base in API_BASES]
        api_data, api_errors = fetch_api_json(api_urls, token)
        errors.extend(api_errors)
        if api_data:
            parsed = parse_warlog_json(api_data, clan_tag)
            if parsed:
                clan_result = parsed.get("clan_result")
                participants = parsed.get("participants") or []
                source = "api"
            else:
                errors.append("API parsing mismatch voor war log.")

    if clan_result is None and not participants:
        try:
            html = fetch_html(WAR_LOG_URL_TEMPLATE.format(tag=normalize_tag(clan_tag)))
            parsed = parse_warlog_html(html, clan_tag, clan_name)
            if parsed:
                clan_result = parsed.get("clan_result") or parsed.get("latest_week")
                participants = parsed.get("participants") or []
            else:
                errors.append("HTML parsing mismatch voor war log.")
        except Exception as exc:
            errors.append(str(exc))

    return build_end_war_payload(clan_tag, clan_name, clan_result, participants, errors, source)
