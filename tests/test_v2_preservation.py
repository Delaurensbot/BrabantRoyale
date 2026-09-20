"""Guard the UI experiment's promise to retain the existing dashboard."""
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PageInventory(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.ids = []
        self.copy_targets = []
        self.cards = 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        if "data-copy" in attrs:
            self.copy_targets.append(attrs["data-copy"])
        if "card" in attrs.get("class", "").split():
            self.cards += 1


def test_v2_retains_every_dashboard_target_and_copy_action():
    original = PageInventory((ROOT / "index.html").read_text(encoding="utf-8"))
    v2 = PageInventory((ROOT / "v2/index.html").read_text(encoding="utf-8"))
    assert set(original.ids) <= set(v2.ids)
    assert len(v2.ids) == len(set(v2.ids)), "Duplicate IDs break existing rendering"
    assert Counter(original.copy_targets) == Counter(v2.copy_targets)
    assert original.cards == v2.cards


def test_v2_preserves_existing_renderers_and_calculations():
    original = (ROOT / "index.html").read_text(encoding="utf-8")
    v2 = (ROOT / "v2/index.html").read_text(encoding="utf-8")
    # Every helper from the original script remains byte-equivalent, including
    # official/scraper source selection, formatting, sorting and copy actions.
    original_helpers = original.split("<script>", 1)[1].split("  async function fetchData()", 1)[0]
    v2_helpers = v2.split("<script>", 1)[1].split("  let v2RequestId", 1)[0]
    assert original_helpers == v2_helpers
