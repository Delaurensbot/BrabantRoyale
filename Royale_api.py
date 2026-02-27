# royaleapi_war_dashboard.py
# Terminal dashboard for RoyaleAPI war race:
# 1) Clan overview (decks used today, avg medals/deck, projected medals, boat, medals, trophies)
# 2) Insights (projected ranking + our situation)
# 3) Player list (only CURRENT clan members) + battles left today + risk left attacks + duels left
# 4) Short story (Discord-friendly) with character limit

import argparse
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

import requests
from bs4 import BeautifulSoup


DEFAULT_CLAN_TAG = "9YP8UY"
CLAN_CONFIGS = {
    DEFAULT_CLAN_TAG: {"name": "Brabant Royale"},
    "GPCLVLPP": {"name": "Brabant Royale 2"},
}


# -----------------------------
# Networking
# -----------------------------
def fetch_html(url: str, timeout: int = 25) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "nl,en-US;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} bij ophalen van {url}")
    return r.text


def clean_text(s: str) -> str:
    s = s.replace("\xa0", " ").strip()
    s = re.sub(r"\s+", " ", s)
    return s


# -----------------------------
# Clan member filtering
# -----------------------------
def normalize_tag(tag: str) -> str:
    tag = tag.strip()
    tag = tag.replace("%23", "").replace("#", "")
    tag = re.sub(r"[^A-Za-z0-9]", "", tag)
    return tag.upper()


def get_clan_config(tag: Optional[str] = None) -> Dict[str, str]:
    normalized = normalize_tag(tag or DEFAULT_CLAN_TAG) or DEFAULT_CLAN_TAG
    config = CLAN_CONFIGS.get(normalized, CLAN_CONFIGS[DEFAULT_CLAN_TAG])

    return {
        "tag": normalized,
        "name": config.get("name", ""),
        "race_url": f"https://royaleapi.com/clan/{normalized}/war/race",
        "clan_url": f"https://royaleapi.com/clan/{normalized}",
        "analytics_url": f"https://royaleapi.com/clan/{normalized}/war/analytics",
        "join_history_url": f"https://royaleapi.com/clan/{normalized}/history/join-leave",
    }


DEFAULT_CLAN_CONFIG = get_clan_config(DEFAULT_CLAN_TAG)
RACE_URL_DEFAULT = DEFAULT_CLAN_CONFIG["race_url"]
CLAN_URL_DEFAULT = DEFAULT_CLAN_CONFIG["clan_url"]
OUR_CLAN_NAME_DEFAULT = DEFAULT_CLAN_CONFIG["name"]


def extract_player_tag_from_href(href: str) -> Optional[str]:
    if not href:
        return None
    m = re.search(r"/player/([^/?#]+)", href)
    if not m:
        return None
    return normalize_tag(m.group(1))


def fetch_clan_members(clan_url: str) -> Tuple[Set[str], Set[str]]:
    """
    Returns:
      tags: set of player tags (best signal)
      names: set of player names (fallback)
    """
    html = fetch_html(clan_url)
    soup = BeautifulSoup(html, "html.parser")

    tags: Set[str] = set()
    names: Set[str] = set()

    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if "/player/" not in href:
            continue

        tag = extract_player_tag_from_href(href)
        if tag:
            tags.add(tag)

        name = a.get_text(" ", strip=True)
        if name:
            names.add(name)

    return tags, names


# -----------------------------
# Player rows parsing (race participants table)
# -----------------------------
def pick_header_row(table) -> List[str]:
    for tr in table.find_all("tr"):
        ths = tr.find_all("th")
        if ths:
            return [clean_text(th.get_text(" ", strip=True)) for th in ths]
    return []


def score_player_table(table) -> int:
    headers = pick_header_row(table)
    joined = " ".join(h.lower() for h in headers)

    trs = table.find_all("tr")
    data_rows = [tr for tr in trs if tr.find_all("td")]
    if len(data_rows) < 10:
        return -1

    score = len(data_rows)

    for kw in ["role", "fame", "deck", "decks", "today"]:
        if kw in joined:
            score += 25

    first_tds = data_rows[0].find_all("td")
    if first_tds:
        c0 = clean_text(first_tds[0].get_text(" ", strip=True))
        if re.fullmatch(r"\d+", c0 or ""):
            score += 50

    return score


def find_player_table(soup: BeautifulSoup):
    best = None
    best_score = -1
    for t in soup.find_all("table"):
        s = score_player_table(t)
        if s > best_score:
            best = t
            best_score = s
    return best


def parse_player_rows_from_race_soup(soup: BeautifulSoup) -> List[Dict]:
    """
    Output row dict keys:
      rank, tag, name, role, decks_used_today, decks_total_so_far, boat_attacks, fame
    """
    table = find_player_table(soup)
    if not table:
        return []

    rows: List[Dict] = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue

        rank_text = clean_text(tds[0].get_text(" ", strip=True))
        if not re.fullmatch(r"\d+", rank_text or ""):
            continue
        rank = int(rank_text)

        tag = ""
        name = ""
        a_player = None
        for a in tr.find_all("a", href=True):
            if "/player/" in a.get("href", ""):
                a_player = a
                break
        if a_player:
            tag_found = extract_player_tag_from_href(a_player.get("href", ""))
            if tag_found:
                tag = tag_found
            name = clean_text(a_player.get_text(" ", strip=True))

        row_text = clean_text(tr.get_text(" ", strip=True))

        role = ""
        decks_used_today: Optional[int] = None
        decks_total_so_far: Optional[int] = None
        boat_attacks: Optional[int] = None
        fame: Optional[int] = None

        m = re.search(
            r"(?P<name>.+?)\s+(?P<role>Leader|Co-leader|Elder|Member|--)\s+"
            r"(?P<today>\d+)\s+(?P<total>\d+)\s+(?P<boat>\d+)\s+(?P<fame>\d+)\s*$",
            row_text,
        )
        if m:
            if not name:
                name = clean_text(m.group("name"))
            role = m.group("role").strip()
            decks_used_today = int(m.group("today"))
            decks_total_so_far = int(m.group("total"))
            boat_attacks = int(m.group("boat"))
            fame = int(m.group("fame"))
        else:
            ints = [int(x) for x in re.findall(r"\d+", row_text)]
            if len(ints) >= 4:
                decks_used_today, decks_total_so_far, boat_attacks, fame = (
                    ints[-4],
                    ints[-3],
                    ints[-2],
                    ints[-1],
                )

            for rname in ["Leader", "Co-leader", "Elder", "Member", "--"]:
                if f" {rname} " in f" {row_text} ":
                    role = rname
                    break

            if not name:
                name = row_text

        rows.append(
            {
                "rank": rank,
                "tag": tag,
                "name": name,
                "role": role,
                "decks_used_today": decks_used_today if decks_used_today is not None else "",
                "decks_total_so_far": decks_total_so_far if decks_total_so_far is not None else "",
                "boat_attacks": boat_attacks if boat_attacks is not None else "",
                "fame": fame if fame is not None else "",
            }
        )

    return rows


def dedupe_rows(rows: List[Dict]) -> List[Dict]:
    seen: Set[str] = set()
    out: List[Dict] = []
    for r in rows:
        key = (r.get("tag") or r.get("name") or "").strip()
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


# -----------------------------
# Rendering player table + battles left + duels left
# -----------------------------
def render_player_table(rows: List[Dict]) -> str:
    headers = ["#", "Name", "Role", "Today/Total", "Boat", "Fame"]

    def s(x) -> str:
        return str(x) if x is not None else ""

    name_w = max([len(s(r.get("name", ""))) for r in rows] + [len("Name")])
    role_w = max([len(s(r.get("role", ""))) for r in rows] + [len("Role")])
    decks_w = max(
        [
            len(f'{s(r.get("decks_used_today",""))}/{s(r.get("decks_total_so_far",""))}')
            for r in rows
        ]
        + [len("Today/Total")]
    )
    fame_w = max([len(s(r.get("fame", ""))) for r in rows] + [len("Fame")])

    head = (
        f'{headers[0]:>3} | {headers[1]:<{name_w}} | {headers[2]:<{role_w}} | '
        f'{headers[3]:>{decks_w}} | {headers[4]:>4} | {headers[5]:>{fame_w}}'
    )
    sep = "-" * len(head)

    lines = [head, sep]
    for r in rows:
        decks = f'{s(r.get("decks_used_today",""))}/{s(r.get("decks_total_so_far",""))}'
        lines.append(
            f'{int(r["rank"]):>3} | {s(r.get("name","")):<{name_w}} | {s(r.get("role","")):<{role_w}} | '
            f'{decks:>{decks_w}} | {s(r.get("boat_attacks","")):>4} | {s(r.get("fame","")):>{fame_w}}'
        )

    return "\n".join(lines)


def attacks_left_today(row: Dict) -> Optional[int]:
    try:
        used = int(row.get("decks_used_today", 0))
    except Exception:
        return None
    left = 4 - used
    if left < 0:
        left = 0
    if left > 4:
        left = 4
    return left


def compute_battles_left(rows: List[Dict]) -> int:
    total = 0
    for r in rows:
        left = attacks_left_today(r)
        if left is None:
            continue
        total += left
    return total


def compute_duels_left(rows: List[Dict]) -> int:
    """
    Jouw definitie:
    Als een speler 3 of 4 aanvallen open heeft, kan die nog een duel spelen.
    (Een duel kan tot 3 decks kosten, daarom >=3.)
    """
    count = 0
    for r in rows:
        left = attacks_left_today(r)
        if left is None:
            continue
        if left >= 3:
            count += 1
    return count


def compute_total_players_participated(rows: List[Dict]) -> int:
    """
    Aantal unieke spelers uit de huidige clan die vandaag minimaal 1 deck gebruikten.
    Gebaseerd op decks_used_today uit de players tabel.
    """
    count = 0
    for r in rows:
        try:
            used_today = int(r.get("decks_used_today", 0) or 0)
        except Exception:
            continue
        if used_today >= 1:
            count += 1
    return count


def bucket_open_players(rows: List[Dict]) -> Dict[int, List[str]]:
    buckets: Dict[int, List[str]] = {4: [], 3: [], 2: [], 1: [], 0: []}
    for r in rows:
        left = attacks_left_today(r)
        if left is None:
            continue
        name = (r.get("name") or "").strip()
        if not name:
            continue
        buckets[left].append(name)
    return buckets


def render_battles_left_today(rows: List[Dict]) -> str:
    buckets = bucket_open_players(rows)
    out: List[str] = []
    out.append("Battles left (today):")

    any_added = False
    for k in [4, 3, 2, 1]:
        names = buckets.get(k, [])
        if not names:
            continue
        any_added = True
        out.append("")
        out.append(f"{k} attack{'s' if k != 1 else ''} left:")
        for n in names:
            out.append(f"- {n}")

    if not any_added:
        out.append("")
        out.append("Iedereen is klaar voor vandaag.")
    return "\n".join(out)


def render_risk_left_attacks(rows: List[Dict]) -> str:
    buckets = bucket_open_players(rows)
    out: List[str] = []
    out.append("Spelers met nog losse aanvallen:")

    any_added = False
    for k in [3, 2, 1]:
        names = buckets.get(k, [])
        if not names:
            continue
        any_added = True
        out.append("")
        out.append(f"{k} attack{'s' if k != 1 else ''} left:")
        for n in names:
            out.append(f"- {n}")

    if not any_added:
        out.append("")
        out.append("Geen risico spelers gevonden (niemand met 1-3 open).")
    return "\n".join(out)


def render_high_fame_players(
    soup: BeautifulSoup, rows: List[Dict], threshold: int = 3000
) -> str:
    day_num = parse_day_number(soup)

    if day_num != 4:
        return ""

    high_famers = []
    for r in rows:
        fame = r.get("fame")
        name = (r.get("name") or "").strip()
        if fame is None or not name:
            continue
        try:
            fame_val = int(fame)
        except (TypeError, ValueError):
            continue
        if fame_val >= threshold:
            high_famers.append((name, fame_val))

    high_famers.sort(key=lambda item: item[1], reverse=True)

    out: List[str] = []
    out.append("Spelers 3000+ 🌟:")

    if not high_famers:
        out.append("- Geen spelers boven de 3000 fame.")
        return "\n".join(out)

    out.append(f"- Aantal: {len(high_famers)}")
    out.append("")

    for name, fame_val in high_famers:
        out.append(f"- {name}: {fame_val}")

    return "\n".join(out)


def collect_day1_high_famers(
    soup: BeautifulSoup, rows: List[Dict], threshold: int = 800
) -> List[Tuple[str, int]]:
    day_num = parse_day_number(soup)

    if day_num != 1:
        return []

    high_famers: List[Tuple[str, int]] = []
    for r in rows:
        fame = r.get("fame")
        name = (r.get("name") or "").strip()

        if fame is None or not name:
            continue

        try:
            fame_val = int(fame)
        except (TypeError, ValueError):
            continue

        if fame_val >= threshold:
            high_famers.append((name, fame_val))

    high_famers.sort(key=lambda item: item[1], reverse=True)

    return high_famers


def render_day1_high_fame_players(
    soup: BeautifulSoup, rows: List[Dict], threshold: int = 800
) -> str:
    high_famers = collect_day1_high_famers(soup, rows, threshold)

    if not high_famers:
        return ""

    out: List[str] = []
    out.append("Spelers 800+ 🏅")
    out.append(f"- Aantal: {len(high_famers)}")
    out.append("")

    for name, fame_val in high_famers:
        out.append(f"- {name}: {fame_val}")

    return "\n".join(out)


def render_day4_last_chance_players(
    soup: BeautifulSoup, rows: List[Dict], min_fame: int = 2100
) -> str:
    day_num = parse_day_number(soup)

    out: List[str] = []
    if day_num != 4:
        return ""

    out.append("Spelers die nog 3k kunnen halen! 🌟")

    candidates: List[Tuple[str, int]] = []
    for r in rows:
        name = (r.get("name") or "").strip()
        fame = r.get("fame")
        left = attacks_left_today(r)

        if not name or fame is None or left != 4:
            continue

        try:
            fame_val = int(fame)
        except (TypeError, ValueError):
            continue

        if fame_val >= min_fame:
            candidates.append((name, fame_val))

    if not candidates:
        out.append("- Niemand gevonden met 0/4 en 2100+ punten.")
        return "\n".join(out)

    candidates.sort(key=lambda item: item[1], reverse=True)

    for name, fame_val in candidates:
        out.append(f"- {name}: {fame_val}")

    return "\n".join(out)


# -----------------------------
# Clan overview parsing (DIV layout)
# -----------------------------
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


def extract_decks_used_total(text: str) -> Tuple[Optional[int], Optional[int]]:
    m = re.search(r"(\d+)\s*/\s*(\d+)", text)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def extract_projected_medals(text: str) -> Optional[int]:
    m = re.search(r"(?:→|->)\s*([0-9]+)", text)
    if m:
        return int(m.group(1))
    return None


def first_int(text: str) -> Optional[int]:
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else None


def first_float(text: str) -> Optional[float]:
    m = re.search(r"(\d+(?:\.\d+)?)", text)
    return float(m.group(1)) if m else None


def class_has(cls_list: List[str], token: str) -> bool:
    return token in cls_list if cls_list else False


def parse_day_label(soup: BeautifulSoup) -> Optional[str]:
    txt = soup.get_text(" ", strip=True)
    m = re.search(r"\bDay\s+(\d+)\b", txt)
    if m:
        return f"Day {m.group(1)}"
    return None


def translate_day_label(label: Optional[str]) -> Optional[str]:
    if not label:
        return None
    return label.replace("Day", "Dag", 1)


def parse_day_number(soup: BeautifulSoup) -> Optional[int]:
    label = parse_day_label(soup)
    if not label:
        return None
    m = re.search(r"(\d+)", label)
    if not m:
        return None
    return int(m.group(1))


def calculate_avg_medals_per_deck(
    current_medals: Optional[int], decks_used_today: Optional[int], fallback: Optional[float]
) -> Optional[float]:
    """Return medals per deck if data is available, otherwise return the fallback."""

    if current_medals is not None and decks_used_today:
        return current_medals / decks_used_today
    return fallback


def parse_clan_overview_from_race_soup_div(soup: BeautifulSoup) -> List[ClanOverview]:
    standings_divs = soup.find_all("div", class_=lambda c: c and "standings" in c.split())
    if not standings_divs:
        return []

    standings = max(standings_divs, key=lambda d: len(d.get_text(" ", strip=True)))

    clan_rows = standings.find_all(
        "a",
        class_=lambda c: c and ("clan" in c.split()) and ("row" in c.split()),
        href=True,
    )
    if not clan_rows:
        return []

    clans: List[ClanOverview] = []

    for a in clan_rows:
        name = ""
        summary = a.find("div", class_=lambda c: c and "summary" in c.split())
        if summary:
            lines = [x.strip() for x in summary.get_text("\n", strip=True).split("\n") if x.strip()]
            if lines:
                name = lines[0]
        if not name:
            raw_lines = [x.strip() for x in a.get_text("\n", strip=True).split("\n") if x.strip()]
            for ln in raw_lines:
                if not re.fullmatch(r"[0-9\s/.\-→]+", ln):
                    name = ln
                    break

        outline = a.find("div", class_=lambda c: c and ("cw2_standing_outline" in c or "standing_outline" in c))
        used = total = None
        avg = None
        projected = None

        if outline:
            decks_el = outline.find("div", class_=lambda c: c and "decks_used_today" in c.split())
            if decks_el:
                used, total = extract_decks_used_total(clean_text(decks_el.get_text(" ", strip=True)))

            avg_el = outline.find("div", class_=lambda c: c and "medal_avg" in c.split())
            if avg_el:
                avg = first_float(clean_text(avg_el.get_text(" ", strip=True)))

            projected = extract_projected_medals(clean_text(outline.get_text(" ", strip=True)))
        else:
            row_text = clean_text(a.get_text(" ", strip=True))
            used, total = extract_decks_used_total(row_text)
            projected = extract_projected_medals(row_text)

        digits: List[int] = []
        for div in a.find_all("div"):
            classes = div.get("class") or []
            if not (class_has(classes, "item") and class_has(classes, "value")):
                continue
            txt = clean_text(div.get_text(" ", strip=True))
            if not re.fullmatch(r"\d+", txt or ""):
                continue
            if outline and div in outline.find_all("div"):
                continue
            digits.append(int(txt))

        boat_points = current_medals = trophies = None
        if len(digits) >= 3:
            boat_points, current_medals, trophies = digits[0], digits[1], digits[2]

        avg = calculate_avg_medals_per_deck(current_medals, used, avg)

        if not name:
            continue
        clans.append(
            ClanOverview(
                name=name,
                decks_used_today=used,
                decks_total_today=total,
                avg_medals_per_deck=avg,
                projected_medals=projected,
                boat_points=boat_points,
                current_medals=current_medals,
                trophies=trophies,
            )
        )

    out: List[ClanOverview] = []
    seen: Set[str] = set()
    for c in clans:
        key = c.name.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(c)

    return out


def parse_clan_overview_from_race_soup_table(soup: BeautifulSoup) -> List[ClanOverview]:
    clans: List[ClanOverview] = []

    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue

        row_text = clean_text(tr.get_text(" ", strip=True))
        used, total = extract_decks_used_total(row_text)
        proj = extract_projected_medals(row_text)

        if used is None or total is None:
            continue

        td0_lines = [x.strip() for x in tds[0].get_text("\n", strip=True).split("\n") if x.strip()]
        clan_name = td0_lines[0] if td0_lines else ""

        td0_flat = clean_text(tds[0].get_text(" ", strip=True))
        floats = [float(x) for x in re.findall(r"\d+\.\d+", td0_flat)]
        avg = floats[0] if floats else None

        boat_points = first_int(clean_text(tds[1].get_text(" ", strip=True)))
        current_medals = first_int(clean_text(tds[2].get_text(" ", strip=True)))
        avg = calculate_avg_medals_per_deck(current_medals, used, avg)
        trophies = first_int(clean_text(tds[3].get_text(" ", strip=True)))

        clans.append(
            ClanOverview(
                name=clan_name,
                decks_used_today=used,
                decks_total_today=total,
                avg_medals_per_deck=avg,
                projected_medals=proj,
                boat_points=boat_points,
                current_medals=current_medals,
                trophies=trophies,
            )
        )

    out: List[ClanOverview] = []
    seen: Set[str] = set()
    for c in clans:
        key = c.name.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(c)

    return out


def parse_clan_overview_from_race_soup_text(soup: BeautifulSoup) -> List[ClanOverview]:
    tokens = [clean_text(t) for t in soup.stripped_strings if clean_text(t)]
    if not tokens:
        return []

    start = 0
    for i in range(len(tokens) - 3):
        if (
            tokens[i].lower() == "clan"
            and tokens[i + 1].lower() == "boat"
            and tokens[i + 2].lower() == "medal"
            and tokens[i + 3].lower() == "trophy"
        ):
            start = i + 4
            break

    rows: List[ClanOverview] = []
    i = start

    def parse_int(value: str) -> Optional[int]:
        if re.fullmatch(r"\d+", value or ""):
            return int(value)
        return None

    while i < len(tokens):
        name = tokens[i]
        if name.lower() in {"clan", "boat", "medal", "trophy"}:
            i += 1
            continue
        if re.fullmatch(r"\d+\s*/\s*\d+", name):
            i += 1
            continue
        if re.fullmatch(r"\d+\.\d+", name):
            i += 1
            continue
        if name in {"→", "->"}:
            i += 1
            continue
        if parse_int(name) is not None:
            i += 1
            continue
        if not re.search(r"[A-Za-zÀ-ÖØ-öø-ÿА-Яа-я]", name):
            i += 1
            continue
        if len(name.strip()) < 2:
            i += 1
            continue

        boat = medal = trophy = None
        used = total = projected = None
        avg = None

        j = i + 1
        ints: List[int] = []
        while j < len(tokens) and len(ints) < 3:
            val = parse_int(tokens[j])
            if val is not None:
                ints.append(val)
            j += 1

        if len(ints) < 3:
            i += 1
            continue

        boat, medal, trophy = ints[0], ints[1], ints[2]

        for k in range(i + 1, min(i + 20, len(tokens))):
            tk = tokens[k]
            m_decks = re.fullmatch(r"(\d+)\s*/\s*(\d+)", tk)
            if m_decks:
                used = int(m_decks.group(1))
                total = int(m_decks.group(2))

            m_avg = re.fullmatch(r"\d+\.\d+", tk)
            if m_avg and avg is None:
                avg = float(tk)

            m_proj = re.search(r"(?:→|->)\s*(\d+)", tk)
            if m_proj:
                projected = int(m_proj.group(1))
            elif tk in {"→", "->"} and k + 1 < len(tokens):
                nxt = parse_int(tokens[k + 1])
                if nxt is not None:
                    projected = nxt

            if used is not None and avg is not None and projected is not None:
                break

        if used is None and avg is None and projected is None:
            i += 1
            continue

        rows.append(
            ClanOverview(
                name=name,
                decks_used_today=used,
                decks_total_today=total,
                avg_medals_per_deck=avg,
                projected_medals=projected,
                boat_points=boat,
                current_medals=medal,
                trophies=trophy,
            )
        )
        i += 1

    out: List[ClanOverview] = []
    seen: Set[str] = set()
    for c in rows:
        key = c.name.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(c)

    return out


def parse_clan_overview_from_race_soup(soup: BeautifulSoup) -> List[ClanOverview]:
    clans = parse_clan_overview_from_race_soup_div(soup)
    if clans:
        return clans
    clans = parse_clan_overview_from_race_soup_table(soup)
    if clans:
        return clans
    return parse_clan_overview_from_race_soup_text(soup)


def render_clan_overview_table(clans: List[ClanOverview]) -> str:
    if not clans:
        return "Clan overview: (niet gevonden op deze pagina)"

    def projected_from_avg(avg: Optional[float]) -> Optional[int]:
        if avg is None:
            return None
        return int(avg * 200)

    for clan in clans:
        if clan.projected_medals is None:
            proj = projected_from_avg(clan.avg_medals_per_deck)
            if proj is not None:
                clan.projected_medals = proj

    def s(x) -> str:
        return "" if x is None else str(x)

    def sf(x: Optional[float]) -> str:
        return "" if x is None else f"{x:.2f}"

    name_w = max([len(c.name) for c in clans] + [len("Clan")])
    decks_w = max([len(f"{s(c.decks_used_today)}/{s(c.decks_total_today)}") for c in clans] + [len("Decks")])
    avg_w = max([len(sf(c.avg_medals_per_deck)) for c in clans] + [len("Avg/deck")])
    proj_w = max([len(s(c.projected_medals)) for c in clans] + [len("Projected")])
    boat_w = max([len(s(c.boat_points)) for c in clans] + [len("Boat")])
    medal_w = max([len(s(c.current_medals)) for c in clans] + [len("Medals")])
    head = (
        f'{"Clan":<{name_w}} | {"Decks":>{decks_w}} | {"Avg/deck":>{avg_w}} | {"Projected":>{proj_w}} | '
        f'{"Boat":>{boat_w}} | {"Medals":>{medal_w}}'
    )
    sep = "-" * len(head)

    clans = sorted(
        clans,
        key=lambda c: (
            -(c.current_medals if c.current_medals is not None else -1),
            c.name.lower(),
        ),
    )

    lines = ["Clan overview:", head, sep]
    for c in clans:
        decks = f"{s(c.decks_used_today)}/{s(c.decks_total_today)}"
        lines.append(
            f"{c.name:<{name_w}} | {decks:>{decks_w}} | {sf(c.avg_medals_per_deck):>{avg_w}} | "
            f"{s(c.projected_medals):>{proj_w}} | {s(c.boat_points):>{boat_w}} | {s(c.current_medals):>{medal_w}}"
        )
    return "\n".join(lines)


def render_clan_avg_projection(clans: List[ClanOverview]) -> str:
    if not clans:
        return "Clan avg/projection: (niet gevonden op deze pagina)"

    name_w = max([len(c.name) for c in clans] + [len("Clan name")])
    avg_w = max([
        len(f"{c.avg_medals_per_deck:.2f}") if c.avg_medals_per_deck is not None else 0
        for c in clans
    ] + [len("Avg")])
    proj_w = max([len(str(c.projected_medals or "")) for c in clans] + [len("Projected")])

    lines = ["Clan name avg projected:"]
    header = f"{'Clan name':<{name_w}} | {'Avg':>{avg_w}} | {'Projected':>{proj_w}}"
    lines.append(header)
    lines.append("-" * len(header))

    for c in clans:
        avg_txt = "" if c.avg_medals_per_deck is None else f"{c.avg_medals_per_deck:.2f}"
        proj_txt = "" if c.projected_medals is None else str(c.projected_medals)
        lines.append(f"{c.name:<{name_w}} | {avg_txt:>{avg_w}} | {proj_txt:>{proj_w}}")

    return "\n".join(lines)


def get_projected_ranking(clans: List[ClanOverview]) -> List[ClanOverview]:
    sortable = [c for c in clans if c.projected_medals is not None]
    sortable.sort(key=lambda x: int(x.projected_medals), reverse=True)
    return sortable


def render_clan_insights(clans: List[ClanOverview], our_clan_name: str) -> str:
    if not clans:
        return "Insights: (geen clan overview beschikbaar)"

    lines: List[str] = []
    lines.append("Insights:")

    finished = []
    for c in clans:
        if c.decks_used_today is not None and c.decks_total_today is not None:
            if c.decks_used_today >= c.decks_total_today:
                finished.append(c.name)

    lines.append("")
    lines.append("Clans finished (all decks used today):")
    if finished:
        for n in finished:
            lines.append(f"- {n}")
    else:
        lines.append("- (nog niemand)")

    sortable = get_projected_ranking(clans)

    lines.append("")
    lines.append("Projected ranking (high to low):")
    if sortable:
        for i, c in enumerate(sortable, start=1):
            lines.append(f"{i:>2}. {c.name} -> {c.projected_medals}")
    else:
        lines.append("(projected medals niet gevonden)")

    our = None
    for c in clans:
        if c.name.strip().lower() == our_clan_name.strip().lower():
            our = c
            break

    if our and our.current_medals is not None and our.decks_used_today is not None and our.decks_total_today is not None:
        remaining = max(0, int(our.decks_total_today) - int(our.decks_used_today))

        lines.append("")
        lines.append(f"Our clan: {our.name}")
        lines.append(f"- Current medals: {our.current_medals}")
        lines.append(f"- Decks used today: {our.decks_used_today}/{our.decks_total_today}")
        lines.append(f"- Decks remaining today: {remaining}")

        if remaining > 0 and our.projected_medals is not None:
            higher = [
                c for c in sortable
                if c.projected_medals is not None and c.projected_medals > our.projected_medals
            ]
            if higher:
                target = higher[-1]
                needed_total = int(target.projected_medals) + 1
                needed_per_deck = (needed_total - int(our.current_medals)) / remaining
                lines.append("")
                lines.append("To beat the closest clan above us (by projected medals):")
                lines.append(f"- Target: {target.name} projected {target.projected_medals}")
                lines.append(f"- Needed average medals per remaining deck: {needed_per_deck:.2f}")
            else:
                lines.append("")
                lines.append("We are not behind anyone on projected medals (or projected missing).")

    return "\n".join(lines)


# -----------------------------
# Clan Stats block + short story
# -----------------------------
def find_our_clan(clans: List[ClanOverview], our_clan_name: str) -> Optional[ClanOverview]:
    for c in clans:
        if c.name.strip().lower() == our_clan_name.strip().lower():
            return c
    return None


def render_clan_stats_block(
    soup: BeautifulSoup,
    clans: List[ClanOverview],
    our_clan_name: str,
    members_rows: List[Dict],
) -> str:
    day = parse_day_label(soup)
    our = find_our_clan(clans, our_clan_name)
    ranking = get_projected_ranking(clans)

    battles_left = compute_battles_left(members_rows)
    duels_left = compute_duels_left(members_rows)
    total_players_participated = compute_total_players_participated(members_rows)

    out: List[str] = []
    out.append("Clan Stats:")

    if day:
        out.append(f"- {day}")

    if our and our.avg_medals_per_deck is not None:
        out.append(f"- Avg medals/deck: {our.avg_medals_per_deck:.2f}")

    out.append(f"- Battles left: {battles_left}")
    out.append(f"- Duels left: {duels_left}")
    out.append(f"- Total players participated: {total_players_participated}")

    if our and our.projected_medals is not None and ranking:
        pos = 1
        for i, c in enumerate(ranking, start=1):
            if c.name.strip().lower() == our.name.strip().lower():
                pos = i
                break
        out.append(f"- Projected: {our.projected_medals} ({pos}e)")

    if our and our.current_medals is not None and our.decks_used_today is not None and our.decks_total_today is not None:
        remaining = max(0, int(our.decks_total_today) - int(our.decks_used_today))
        out.append(f"- Decks: {our.decks_used_today}/{our.decks_total_today} (open {remaining})")
        out.append(f"- Current medals: {our.current_medals}")

    return "\n".join(out)


def build_short_story(
    soup: BeautifulSoup,
    clans: List[ClanOverview],
    our_clan_name: str,
    members_rows: List[Dict],
    max_chars: int,
) -> str:
    day_label = translate_day_label(parse_day_label(soup))
    our = find_our_clan(clans, our_clan_name)
    ranking = get_projected_ranking(clans)

    # Determine position based on projected medals
    pos = None
    if our and our.projected_medals is not None and ranking:
        for i, c in enumerate(ranking, start=1):
            if c.name.strip().lower() == our.name.strip().lower():
                pos = i
                break

    # Determine lead/deficit based on average medals per deck
    avg_sorted = [c for c in clans if c.avg_medals_per_deck is not None]
    avg_sorted.sort(key=lambda c: c.avg_medals_per_deck or 0, reverse=True)
    gap_line = ""
    if our and our.avg_medals_per_deck is not None and avg_sorted:
        our_idx = next(
            (i for i, c in enumerate(avg_sorted) if c.name.strip().lower() == our.name.strip().lower()),
            None,
        )
        if our_idx is not None:
            if our_idx == 0 and len(avg_sorted) > 1:
                lead = our.avg_medals_per_deck - (avg_sorted[1].avg_medals_per_deck or 0)
                gap_line = f"voorsprong op 2e plaats: {lead:.2f}"
            elif our_idx > 0:
                deficit = (avg_sorted[0].avg_medals_per_deck or 0) - our.avg_medals_per_deck
                gap_line = f"achterstand op 1e plaats: {deficit:.2f}"

    # Build compact, NL-focused story
    title = f"{day_label} update" if day_label else "Dag update"
    lines = [f"{title}:"]

    if our and our.decks_used_today is not None and our.decks_total_today is not None:
        lines.append(f"{our.decks_used_today}/{our.decks_total_today} aanvallen")

    if pos is not None:
        lines.append(f"voorspelde uitkomst: {pos}e plek")

    if our and our.avg_medals_per_deck is not None:
        lines.append(f"Avg {our.avg_medals_per_deck:.2f} 🎖")

    if gap_line:
        lines.append(gap_line)

    story = "\n".join(lines).strip()

    if len(story) <= max_chars:
        return story

    return story[: max(0, max_chars - 1)] + "…"


# -----------------------------
# Main
# -----------------------------
def main() -> None:
    ap = argparse.ArgumentParser(description="RoyaleAPI war race dashboard (clan overview + players + story)")
    ap.add_argument("--race-url", default=RACE_URL_DEFAULT)
    ap.add_argument("--clan-url", default=CLAN_URL_DEFAULT)
    ap.add_argument("--our-clan", default=OUR_CLAN_NAME_DEFAULT)
    ap.add_argument("--top", type=int, default=0, help="Toon alleen top N players (0 = alles)")
    ap.add_argument("--story-max", type=int, default=220, help="Max lengte short story (chars)")
    args = ap.parse_args()

    try:
        clan_tags, clan_names = fetch_clan_members(args.clan_url)
    except Exception as e:
        print(f"FOUT: kon clan memberlijst niet ophalen: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        race_html = fetch_html(args.race_url)
    except Exception as e:
        print(f"FOUT: kon race pagina niet ophalen: {e}", file=sys.stderr)
        sys.exit(2)

    race_soup = BeautifulSoup(race_html, "html.parser")

    clans = parse_clan_overview_from_race_soup(race_soup)
    rows = parse_player_rows_from_race_soup(race_soup)

    filtered: List[Dict] = []
    for r in rows:
        tag = normalize_tag(r.get("tag", "")) if r.get("tag") else ""
        name = r.get("name", "")
        if (tag and tag in clan_tags) or (name and name in clan_names):
            filtered.append(r)

    filtered = sorted(filtered, key=lambda x: int(x["rank"]))
    filtered = dedupe_rows(filtered)

    if args.top and args.top > 0:
        filtered = filtered[: args.top]

    print("#")
    print(f"Fetched: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Race URL: {args.race_url}")
    print(f"Clan URL: {args.clan_url}")
    print()

    print(render_clan_overview_table(clans))
    print()
    print(render_clan_insights(clans, args.our_clan))
    print()

    print(render_clan_stats_block(race_soup, clans, args.our_clan, filtered))
    print()

    print("Players (only current clan members):")
    print(render_player_table(filtered))
    print()
    print(render_battles_left_today(filtered))
    print()
    print(render_risk_left_attacks(filtered))
    print()

    day4_block = render_day4_last_chance_players(race_soup, filtered)
    if day4_block:
        print(day4_block)
        print()

    story = build_short_story(race_soup, clans, args.our_clan, filtered, max_chars=args.story_max)
    print("Short story (copy/paste):")
    print(story)
    print()
    print(f"(Length: {len(story)} / {args.story_max})")


if __name__ == "__main__":
    main()
