#!/usr/bin/env python3
"""
Generate CW race stats output and JSON for the Next.js dashboard.
Scraping logic is based on the original cwstats_race script: we parse race rows,
clan stats, and battles-left data from cwstats.com.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests
from bs4 import BeautifulSoup

ROW_RE = re.compile(r"^\s*(\d+)\s+(.*?)\s+(\d+)\s+(\d+)\s+(\d+)\s+([\d.,]+)\s*$")
RANK_RE = re.compile(r"(\d+)(st|nd|rd|th)?", re.IGNORECASE)


@dataclass
class ClanStats:
    avg: Optional[float]
    battles_left: Optional[int]
    duels_left: Optional[int]
    projected_finish_value: Optional[int]
    projected_finish_rank: Optional[str]
    best_possible_finish: Optional[int]
    best_possible_rank: Optional[str]
    worst_possible_finish: Optional[int]
    worst_possible_rank: Optional[str]


def fetch_soup(url: str) -> BeautifulSoup:
    r = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; cwstats-scraper/1.0)"},
        timeout=25,
    )
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "lxml")

    for t in soup(["script", "style", "noscript"]):
        t.decompose()

    return soup


def parse_race_rows(soup: BeautifulSoup):
    rows = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not re.fullmatch(r"/clan/[A-Z0-9]+/race", href):
            continue

        text = " ".join(a.stripped_strings)
        if not text or not text[0].isdigit():
            continue

        m = ROW_RE.match(text)
        if not m:
            continue

        rank = int(m.group(1))
        name = m.group(2).strip()
        trophy = int(m.group(3))
        fame = float(m.group(6).replace(",", "."))

        key = (rank, name, trophy, fame)
        if key in seen:
            continue
        seen.add(key)

        rows.append(
            {
                "rank": rank,
                "name": name,
                "trophy": trophy,
                "fame": fame,
            }
        )

    rows.sort(key=lambda x: x["rank"])
    return rows


def _find_clan_stats_container(soup: BeautifulSoup):
    node = soup.find(string=re.compile(r"\bClan\s+Stats\b", re.IGNORECASE))
    if not node:
        return None

    cur = node.parent
    for _ in range(10):
        if not cur:
            break
        txt = " ".join(cur.stripped_strings).lower()
        if ("battles left" in txt) and ("duels left" in txt) and ("projected finish" in txt):
            return cur
        cur = cur.parent

    return None


def _parse_int(token: Optional[str]) -> Optional[int]:
    if not token:
        return None
    token = token.replace(",", "")
    if token.isdigit():
        return int(token)
    m = re.search(r"\d+", token)
    if m:
        return int(m.group(0))
    return None


def parse_clan_stats(soup: BeautifulSoup) -> Optional[ClanStats]:
    container = _find_clan_stats_container(soup)
    if not container:
        return None

    tokens = [t.strip() for t in container.stripped_strings if t and t.strip()]
    lower = [t.lower() for t in tokens]

    def value_after(label: str) -> Optional[str]:
        ll = label.lower()
        for i, tok in enumerate(lower):
            if tok == ll:
                for j in range(i + 1, min(i + 6, len(tokens))):
                    if re.fullmatch(r"[\d,\.]+", tokens[j]):
                        return tokens[j]
        return None

    def pick_rank_and_value(finish_label: str):
        fl = finish_label.lower()
        for i, tok in enumerate(lower):
            if tok == fl:
                rank = None
                value = None
                if i - 1 >= 0 and re.fullmatch(r"\d+(st|nd|rd|th)", lower[i - 1]):
                    rank = tokens[i - 1]
                if i + 1 < len(tokens) and re.fullmatch(r"[\d,]+", tokens[i + 1]):
                    value = tokens[i + 1]
                else:
                    for j in range(i + 1, min(i + 6, len(tokens))):
                        if re.fullmatch(r"[\d,]+", tokens[j]):
                            value = tokens[j]
                            break
                return rank, value
        return None, None

    avg_raw = value_after("Avg") or value_after("Average")
    battles_raw = value_after("Battles") or value_after("Battles left")
    duels_raw = value_after("Duels") or value_after("Duels left")

    projected_rank, projected_value = pick_rank_and_value("Projected Finish")
    best_rank, best_value = pick_rank_and_value("Best Possible Finish")
    worst_rank, worst_value = pick_rank_and_value("Worst Possible Finish")

    return ClanStats(
        avg=_parse_float(avg_raw),
        battles_left=_parse_int(battles_raw),
        duels_left=_parse_int(duels_raw),
        projected_finish_value=_parse_int(projected_value),
        projected_finish_rank=projected_rank,
        best_possible_finish=_parse_int(best_value),
        best_possible_rank=best_rank,
        worst_possible_finish=_parse_int(worst_value),
        worst_possible_rank=worst_rank,
    )


def _parse_float(token: Optional[str]) -> Optional[float]:
    if not token:
        return None
    token = token.replace(" ", "").replace(",", ".")
    try:
        return float(token)
    except ValueError:
        m = re.search(r"\d+[\.,]\d+", token)
        if m:
            try:
                return float(m.group(0).replace(",", "."))
            except ValueError:
                return None
    return None


def _find_battles_left_table(soup: BeautifulSoup):
    want = {"player", "decks used today"}
    for table in soup.find_all("table"):
        header_cells = table.find_all("th")
        if header_cells:
            headers = [c.get_text(" ", strip=True).lower() for c in header_cells]
        else:
            first_tr = table.find("tr")
            if not first_tr:
                continue
            headers = [c.get_text(" ", strip=True).lower() for c in first_tr.find_all(["td", "th"])]

        header_set = set(h.strip() for h in headers if h.strip())
        if want.issubset(header_set):
            return table
    return None


def parse_battles_left_today(soup: BeautifulSoup):
    table = _find_battles_left_table(soup)
    if not table:
        return None

    header_row = table.find("tr")
    if not header_row:
        return None

    header_cells = header_row.find_all(["th", "td"])
    headers = [c.get_text(" ", strip=True).lower() for c in header_cells]

    def idx_of(name: str):
        name_l = name.lower()
        for i, h in enumerate(headers):
            if h == name_l:
                return i
        return None

    idx_player = idx_of("player")
    idx_today = idx_of("decks used today")
    if idx_player is None or idx_today is None:
        return None

    buckets: Dict[int, List[str]] = {4: [], 3: [], 2: [], 1: []}

    for tr in table.find_all("tr")[1:]:
        tds = tr.find_all(["td", "th"])
        if not tds or len(tds) <= max(idx_player, idx_today):
            continue

        player = tds[idx_player].get_text(" ", strip=True)
        today_raw = tds[idx_today].get_text(" ", strip=True)

        m = re.search(r"\d+", today_raw)
        decks_today = int(m.group(0)) if m else 0

        remaining = 4 - decks_today
        if remaining in buckets:
            if player:
                buckets[remaining].append(player)

    return buckets


def format_race_rows(rows):
    top5 = rows[:5]
    parts = ["Race standings (top 5):"]
    for idx, row in enumerate(top5, 1):
        parts.append(
            f"{idx}. 🏆 {row['name']} — Fame: {row['fame']:.0f} | Trophy: {row['trophy']}"
        )
    if top5:
        avg = sum(r["fame"] for r in top5) / len(top5)
        parts.append(f"Avg fame (top 5): {avg:,.2f}")
    return "\n".join(parts).rstrip()


def format_clan_stats(stats: Optional[ClanStats]):
    if not stats:
        return "Clan Stats:\nGeen data gevonden."

    def fmt_num(val: Optional[float | int]):
        if val is None:
            return "?"
        if isinstance(val, float):
            # European-style decimal formatting (172,34)
            formatted = f"{val:,.2f}"
            return formatted.replace(",", "¤").replace(".", ",").replace("¤", ".")
        return f"{val:,}"

    projected_val = fmt_num(stats.projected_finish_value)
    best_val = fmt_num(stats.best_possible_finish)
    worst_val = fmt_num(stats.worst_possible_finish)

    projected_rank = stats.projected_finish_rank or "?"
    best_rank = stats.best_possible_rank or "?"
    worst_rank = stats.worst_possible_rank or "?"

    lines = [
        "Clan Stats:",
        f"📊 avg {fmt_num(stats.avg)}    ⚔️ Battles left: {fmt_num(stats.battles_left)}    🤝 Duels left: {fmt_num(stats.duels_left)}    🎯 Projected Finish {projected_val} ({projected_rank})",
        f"🏁 Best Possible Finish {best_val} ({best_rank})    💀 Worst Possible Finish {worst_val} ({worst_rank})",
    ]
    return "\n".join(lines)


def format_battles_left_today(buckets):
    if not buckets:
        return "Battles left (today):\nGeen tabel gevonden voor 'Decks Used Today'."

    def block(label, players):
        if not players:
            return ""
        lines = [label]
        for p in players:
            lines.append(f"- {p}")
        return "\n".join(lines)

    parts = ["Battles left (today):"]
    parts.append(block("🟥 4 attacks left:", buckets.get(4, [])))
    parts.append(block("🟧 3 attacks left:", buckets.get(3, [])))
    parts.append(block("🟨 2 attacks left:", buckets.get(2, [])))
    parts.append(block("🟩 1 attack left:", buckets.get(1, [])))

    cleaned = []
    for part in parts:
        if part and part.strip():
            cleaned.append(part.strip())
    return "\n\n".join(cleaned).rstrip()


def build_copy_all_text(race_text: str, clan_text: str, battles_text: str) -> str:
    parts = [race_text.strip(), clan_text.strip(), battles_text.strip()]
    return "\n\n".join([p for p in parts if p]).strip()


def write_files(data: dict, race_text: str, public_dir: Path) -> None:
    public_dir.mkdir(parents=True, exist_ok=True)
    data_path = public_dir / "data.json"
    race_path = public_dir / "race.txt"

    data_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    race_path.write_text(race_text, encoding="utf-8")
    print(f"geschreven: {data_path}")
    print(f"geschreven: {race_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://cwstats.com/clan/9YP8UY/race")
    ap.add_argument("--public-dir", default="public")
    args = ap.parse_args()

    soup = fetch_soup(args.url)

    rows = parse_race_rows(soup)
    if not rows:
        raise SystemExit("Geen race-rows gevonden. Mogelijk is de pagina-structuur veranderd.")

    stats = parse_clan_stats(soup)
    buckets = parse_battles_left_today(soup)

    race_text = format_race_rows(rows)
    stats_text = format_clan_stats(stats)
    battles_left_text = format_battles_left_today(buckets)

    copy_all_text = build_copy_all_text(race_text, stats_text, battles_left_text)

    now = datetime.now(timezone.utc)
    generated_at_iso = now.isoformat().replace("+00:00", "Z")
    generated_at_epoch_ms = int(now.timestamp() * 1000)
    update_interval_seconds = 300

    data = {
        "generated_at_iso": generated_at_iso,
        "generated_at_epoch_ms": generated_at_epoch_ms,
        "update_interval_seconds": update_interval_seconds,
        "sections": {
            "race": {"title": "Race", "text": race_text},
            "clan_stats": {"title": "Clan Stats", "text": stats_text},
            "battles_left": {"title": "Battles left (today)", "text": battles_left_text},
        },
        "copy_all_text": copy_all_text,
    }

    write_files(data, copy_all_text, Path(args.public_dir))

    print("Klaar: data.json en race.txt bijgewerkt.")


if __name__ == "__main__":
    main()
