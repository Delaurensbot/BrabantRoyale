# cwstats_race.py
import re
import argparse
import requests
from bs4 import BeautifulSoup

ROW_RE = re.compile(
    r"^\s*(\d+)\s+(.*?)\s+(\d+)\s+(\d+)\s+(\d+)\s+([\d.,]+)\s*$"
)

def fetch_soup(url: str) -> BeautifulSoup:
    r = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; cwstats-scraper/1.0)"},
        timeout=25,
    )
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "lxml")

    # Verwijder tags die soms ruis geven in text parsing
    for t in soup(["script", "style", "noscript"]):
        t.decompose()

    return soup

def parse_race_rows(soup: BeautifulSoup):
    rows = []
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()

        # Race links lijken op: /clan/9YP8UY/race
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

        # Alleen trophy en fame gebruiken voor output
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

def parse_clan_stats(soup: BeautifulSoup):
    container = _find_clan_stats_container(soup)
    if not container:
        return None

    tokens = [t.strip() for t in container.stripped_strings if t and t.strip()]
    lower = [t.lower() for t in tokens]

    def next_int_after(label: str):
        label_l = label.lower()
        for i, tok in enumerate(lower):
            if tok == label_l:
                for j in range(i + 1, min(i + 6, len(tokens))):
                    if re.fullmatch(r"\d+", tokens[j]):
                        return int(tokens[j])
        return None

    def pick_rank_and_value(finish_label: str):
        fl = finish_label.lower()
        for i, tok in enumerate(lower):
            if tok == fl:
                rank = None
                value = None

                # rank staat vaak direct ervoor (bijv. "3rd")
                if i - 1 >= 0 and re.fullmatch(r"\d+(st|nd|rd|th)", lower[i - 1]):
                    rank = tokens[i - 1]

                # value staat vaak direct erna (bijv. "34,650")
                if i + 1 < len(tokens) and re.fullmatch(r"[\d,]+", tokens[i + 1]):
                    value = tokens[i + 1]
                else:
                    for j in range(i + 1, min(i + 6, len(tokens))):
                        if re.fullmatch(r"[\d,]+", tokens[j]):
                            value = tokens[j]
                            break

                return rank, value
        return None, None

    battles_left = next_int_after("BATTLES LEFT")
    duels_left = next_int_after("DUELS LEFT")

    projected_rank, projected_finish = pick_rank_and_value("Projected Finish")
    best_rank, best_finish = pick_rank_and_value("Best Possible Finish")
    worst_rank, worst_finish = pick_rank_and_value("Worst Possible Finish")

    # Losse avg-waarde (bijv. 172.34) ergens in de container
    avg_value = None
    for t in tokens:
        if re.fullmatch(r"\d+\.\d{2}", t):
            avg_value = t
            break

    return {
        "avg_value": avg_value,
        "battles_left": battles_left,
        "duels_left": duels_left,
        "projected_rank": projected_rank,
        "projected_finish": projected_finish,
        "best_rank": best_rank,
        "best_finish": best_finish,
        "worst_rank": worst_rank,
        "worst_finish": worst_finish,
    }

def _rank_en(rank_str: str | None):
    if not rank_str:
        return ""
    m = re.fullmatch(r"(\d+)(st|nd|rd|th)", rank_str.strip().lower())
    if not m:
        return rank_str
    return f"{m.group(1)}e"

def _avg_to_comma(avg_str: str | None):
    if not avg_str:
        return ""
    try:
        val = float(avg_str.replace(",", "."))
        return f"{val:.2f}".replace(".", ",")
    except ValueError:
        return avg_str.replace(".", ",")

def format_race_rows(rows):
    out = []
    for r in rows:
        out.append(
            f"{r['rank']}. {r['name']}\n"
            f"   🏆 {r['trophy']}\n"
            f"   avg {r['fame']:.2f}\n"
        )
    return "\n".join(out).rstrip()

def format_clan_stats(stats):
    if not stats:
        return ""

    avg = _avg_to_comma(stats.get("avg_value"))
    battles = "" if stats.get("battles_left") is None else str(stats["battles_left"])
    duels = "" if stats.get("duels_left") is None else str(stats["duels_left"])

    proj_finish = stats.get("projected_finish") or ""
    best_finish = stats.get("best_finish") or ""
    worst_finish = stats.get("worst_finish") or ""

    proj_rank = _rank_en(stats.get("projected_rank"))
    best_rank = _rank_en(stats.get("best_rank"))
    worst_rank = _rank_en(stats.get("worst_rank"))

    line1 = (
        "Clan Stats:\n"
        f"📊 avg {avg}    ⚔️ Battles left: {battles}    🤝 Duels left: {duels}    "
        f"🎯 Projected Finish {proj_finish} ({proj_rank})"
    )
    line2 = (
        f"🏁 Best Possible Finish {best_finish} ({best_rank})    "
        f"💀 Worst Possible Finish {worst_finish} ({worst_rank})"
    )

    return line1.rstrip() + "\n" + line2.rstrip()

def _find_battles_left_table(soup: BeautifulSoup):
    want = {"player", "decks used today"}
    for table in soup.find_all("table"):
        # headers kunnen in <th> staan, of in de eerste <tr> als <td>
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
    """
    We gebruiken 'Decks Used Today':
    - 4 betekent klaar (0 attacks left)
    - remaining = 4 - decks_used_today
    We tonen alleen remaining 4,3,2,1.
    """
    table = _find_battles_left_table(soup)
    if not table:
        return None

    # bepaal kolom-indexen
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

    buckets = {4: [], 3: [], 2: [], 1: []}

    # rows
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

def format_battles_left_today(buckets):
    if not buckets:
        return ""

    def block(label, players):
        if not players:
            return ""
        lines = [label]
        for p in players:
            lines.append(f"- {p}")
        return "\n".join(lines)

    parts = ["Battles left (today):"]

    # 4 attacks left = 0 decks used today
    parts.append(block("🟥 4 attacks left:", buckets.get(4, [])))
    parts.append(block("🟧 3 attacks left:", buckets.get(3, [])))
    parts.append(block("🟨 2 attacks left:", buckets.get(2, [])))
    parts.append(block("🟩 1 attack left:", buckets.get(1, [])))

    # verwijder lege blocks en dubbele lege regels
    cleaned = []
    for part in parts:
        if part and part.strip():
            cleaned.append(part.strip())
    return "\n\n".join(cleaned).rstrip()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="https://cwstats.com/clan/9YP8UY/race")
    args = ap.parse_args()

    soup = fetch_soup(args.url)

    rows = parse_race_rows(soup)
    if not rows:
        print("Geen race-rows gevonden. Mogelijk is de pagina-structuur veranderd.")
        return

    stats = parse_clan_stats(soup)
    buckets = parse_battles_left_today(soup)

    output_parts = [format_race_rows(rows)]

    stats_text = format_clan_stats(stats)
    if stats_text:
        output_parts.append(stats_text)

    battles_left_text = format_battles_left_today(buckets)
    if battles_left_text:
        output_parts.append(battles_left_text)
    else:
        output_parts.append("Battles left (today):\nGeen tabel gevonden voor 'Decks Used Today'. Mogelijk is de pagina-structuur veranderd.")

    print("\n\n".join(output_parts).rstrip())

if __name__ == "__main__":
    main()
