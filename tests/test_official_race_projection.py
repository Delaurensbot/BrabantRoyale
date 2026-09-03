import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "api" / "test-clan-prototype.py"
SPEC = importlib.util.spec_from_file_location("official_race_projection", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def race_clan(name, tag, fame=800, decks_used=4):
    return {
        "name": name,
        "tag": tag,
        "fame": fame,
        "repairPoints": 0,
        "participants": [{"decksUsedToday": decks_used}],
    }


def test_section_four_uses_four_day_colosseum_projection():
    assert MODULE.is_colosseum_race({"sectionIndex": 4}) is True
    assert MODULE.is_colosseum_race({"sectionIndex": "4"}) is True
    assert MODULE.projection_multiplier_for_race({"sectionIndex": 4}) == 4

    rows = MODULE.build_overview_rows(
        [race_clan("Brabant Royale", "#9YP8UY")],
        projection_multiplier=4,
    )

    assert rows[0]["daily_projected_medals"] == 40000
    assert rows[0]["projected_medals"] == 160000


def test_other_sections_keep_single_day_projection():
    assert MODULE.is_colosseum_race({"sectionIndex": 3}) is False
    assert MODULE.is_colosseum_race({}) is False
    assert MODULE.projection_multiplier_for_race({"sectionIndex": 3}) == 1

    rows = MODULE.build_overview_rows(
        [race_clan("Brabant Royale", "#9YP8UY")],
        projection_multiplier=1,
    )

    assert rows[0]["daily_projected_medals"] == 40000
    assert rows[0]["projected_medals"] == 40000


def test_finish_outlook_uses_same_colosseum_multiplier():
    rows = MODULE.build_overview_rows(
        [
            race_clan("Brabant Royale", "#9YP8UY", fame=800, decks_used=4),
            race_clan("Opponent", "#ABC", fame=600, decks_used=4),
        ],
        projection_multiplier=4,
    )
    players = [{"attacks_left_today": 0, "decks_used_today": 4}]

    outlook = MODULE.build_finish_outlook("9YP8UY", rows, players, projection_multiplier=4)

    assert outlook["projected_finish"] == 160000
    assert outlook["best_finish"] == 160000
    assert outlook["worst_finish"] == 120800
    assert outlook["projection_multiplier"] == 4
    assert outlook["projection_scope"] == "colosseum_4_days"
