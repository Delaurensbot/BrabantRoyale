import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "api" / "test-clan-prototype.py"
SPEC = importlib.util.spec_from_file_location("official_race_projection", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def race_clan(
    name,
    tag,
    daily_fame=800,
    participant_fame=None,
    decks_used_today=4,
    decks_used_total=None,
):
    if participant_fame is None:
        participant_fame = daily_fame
    if decks_used_total is None:
        decks_used_total = decks_used_today
    return {
        "name": name,
        "tag": tag,
        "fame": daily_fame,
        "repairPoints": 0,
        "participants": [
            {
                "fame": participant_fame,
                "repairPoints": 0,
                "decksUsedToday": decks_used_today,
                "decksUsed": decks_used_total,
            }
        ],
    }


def test_regular_river_race_uses_daily_clan_score_and_daily_decks():
    row = MODULE.build_overview_rows(
        [
            race_clan(
                "Brabant Royale",
                "#9YP8UY",
                daily_fame=32275,
                participant_fame=96375,
                decks_used_today=192,
                decks_used_total=573,
            )
        ],
        is_colosseum=False,
        battle_day=3,
    )[0]

    assert row["score_scope"] == "river_race_day"
    assert row["medals"] == 32275
    assert row["decks_used_today"] == 192
    assert row["avg_medals_per_deck"] == 168.10
    assert row["projected_medals"] == 33620


def test_regular_average_is_not_affected_by_missed_decks_on_previous_days():
    rows = MODULE.build_overview_rows(
        [
            race_clan("Fewer earlier", "#ONE", 32275, 70000, 192, 450),
            race_clan("More earlier", "#TWO", 32275, 120000, 192, 750),
        ],
        is_colosseum=False,
        battle_day=3,
    )

    assert {row["avg_medals_per_deck"] for row in rows} == {168.10}
    assert {row["medals"] for row in rows} == {32275}


def test_regular_day_one_can_use_participants_when_clan_total_is_stale():
    row = MODULE.build_overview_rows(
        [race_clan("Brabant Royale", "#9YP8UY", 0, 1475, 7, 7)],
        is_colosseum=False,
        battle_day=1,
    )[0]

    assert row["medals"] == 1475
    assert row["avg_medals_per_deck"] == 210.71


def test_colosseum_detection_uses_period_type_not_section_index():
    assert MODULE.is_colosseum_race({"periodType": "colosseum", "sectionIndex": 1}) is True
    assert MODULE.is_colosseum_race({"periodType": "COLOSSEUM", "sectionIndex": 3}) is True
    assert MODULE.is_colosseum_race({"periodType": "warDay", "sectionIndex": 4}) is False
    assert MODULE.is_colosseum_race({"sectionIndex": 4}) is False


def test_battle_day_comes_from_season_monotonic_period_index():
    assert MODULE.battle_day_for_race({"periodIndex": 10}) == 1
    assert MODULE.battle_day_for_race({"periodIndex": 13}) == 4
    assert MODULE.battle_day_for_race({"periodIndex": 9}) is None
    assert MODULE.battle_day_for_race({}) is None


def test_colosseum_uses_cumulative_score_and_only_projects_remaining_days():
    rows = MODULE.build_overview_rows(
        [race_clan("Brabant Royale", "#9YP8UY", 800, 2400, 4, 12)],
        is_colosseum=True,
        battle_day=3,
    )
    row = rows[0]

    assert row["score_scope"] == "colosseum_cumulative"
    assert row["medals"] == 2400
    assert row["avg_medals_per_deck"] == 200
    assert row["projection_decks_remaining"] == 396
    assert row["projected_medals"] == 81600

    players = [{"attacks_left_today": 0, "decks_used_today": 4}]
    outlook = MODULE.build_finish_outlook("9YP8UY", rows, players, is_colosseum=True)

    assert outlook["projected_finish"] == 81600
    assert outlook["best_finish"] == 81600
    assert outlook["worst_finish"] == 81600
    assert outlook["projection_scope"] == "colosseum_remaining_days"
