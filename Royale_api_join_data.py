#!/usr/bin/env python3
import re
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

CLAN_TAG = "9YP8UY"
JOIN_LEAVE_URL = f"https://royaleapi.com/clan/{CLAN_TAG}/history/join-leave"
PLAYER_URL_TEMPLATE = "https://royaleapi.com/player/{pid}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/138.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "nl,en-US;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}


def fetch_html(session: requests.Session, url: str, timeout: int = 25) -> Tuple[int, str]:
    r = session.get(url, headers=HEADERS, timeout=timeout)
    return r.status_code, r.text


def looks_blocked(html: str) -> bool:
    t = html.lower()
    return (
        ("cloudflare" in t and "attention required" in t)
        or ("just a moment" in t and "cloudflare" in t)
        or ("cf-chl" in t)
        or ("please enable javascript" in t)
        or ("captcha" in t)
    )


def normalize_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text


def parse_last_joins(html: str, limit: int = 10) -> List[Dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")

    # Joins are "positive message" blocks with a green plus icon.
    join_blocks = soup.select("div.ui.attached.icon.positive.message")

    joins: List[Dict[str, str]] = []
    for blk in join_blocks:
        name_el = blk.select_one("div.header")
        ago_el = blk.select_one("div.ago.i18n_duration_short")
        utc_el = blk.select_one("div.utc")

        name = name_el.get_text(strip=True) if name_el else ""
        ago = ago_el.get_text(strip=True) if ago_el else ""
        utc = utc_el.get_text(strip=True) if utc_el else ""

        a = blk.find_parent("a", href=re.compile(r"^/player/"))
        pid = ""
        if a and a.get("href"):
            m = re.search(r"/player/([A-Z0-9]+)", a["href"])
            if m:
                pid = m.group(1)

        if name and pid:
            joins.append(
                {
                    "name": name,
                    "pid": pid,
                    "ago": ago,
                    "utc": utc,
                    "url": PLAYER_URL_TEMPLATE.format(pid=pid),
                }
            )

        if len(joins) >= limit:
            break

    return joins


def parse_experience_level(page_text: str) -> Optional[str]:
    # Example: "Experience Level 63"
    m = re.search(r"\bExperience\s+Level\s+(\d+)\b", page_text, flags=re.IGNORECASE)
    return m.group(1) if m else None


def get_player_acc_level(
    session: requests.Session,
    pid: str,
    cache: Dict[str, str],
) -> str:
    if pid in cache:
        return cache[pid]

    url = PLAYER_URL_TEMPLATE.format(pid=pid)
    status, html = fetch_html(session, url)

    if status != 200 or looks_blocked(html):
        cache[pid] = "-"
        return "-"

    text = normalize_text(html)
    acc = parse_experience_level(text) or "-"
    cache[pid] = acc
    return acc


def print_table(rows: List[Dict[str, str]]) -> None:
    idx_w = 2
    name_w = max(4, min(22, max(len(r["name"]) for r in rows) if rows else 4))
    pid_w = max(8, max(len(r["pid"]) for r in rows) if rows else 8)
    ago_w = max(3, min(12, max(len(r["ago"]) for r in rows) if rows else 3))
    url_w = max(10, min(40, max(len(r["url"]) for r in rows) if rows else 10))

    header = (
        f"{'#':>{idx_w}} | {'Name':<{name_w}} | {'Player ID':<{pid_w}} | "
        f"{'Ago':<{ago_w}} | {'AccLvl':>6} | {'Link':<{url_w}}"
    )
    print(header)
    print("-" * len(header))

    for i, r in enumerate(rows, 1):
        print(
            f"{i:>{idx_w}} | "
            f"{r['name']:<{name_w}} | "
            f"{r['pid']:<{pid_w}} | "
            f"{r['ago']:<{ago_w}} | "
            f"{r.get('acc_lvl','-'):>6} | "
            f"{r['url']:<{url_w}}"
        )


def main() -> int:
    limit = 10
    if len(sys.argv) >= 2:
        try:
            limit = max(1, min(50, int(sys.argv[1])))
        except ValueError:
            pass

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    with requests.Session() as session:
        status, html = fetch_html(session, JOIN_LEAVE_URL)
        if status != 200:
            print("Failed to fetch join-leave page:", status)
            return 1

        if looks_blocked(html):
            print("Blocked by anti-bot (Cloudflare/JS challenge).")
            return 1

        joins = parse_last_joins(html, limit=limit)

        acc_cache: Dict[str, str] = {}
        for r in joins:
            r["acc_lvl"] = get_player_acc_level(session, r["pid"], acc_cache)

    print("#")
    print("Fetched:", now)
    print("URL:", JOIN_LEAVE_URL)
    print()
    print("Last joins (with account level + link):")
    print_table(joins)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
