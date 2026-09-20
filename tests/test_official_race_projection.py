import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "api" / "test-clan-prototype.py"
SPEC = importlib.util.spec_from_file_location("official_race_projection", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def race_clan(
    name,
    tag,
    boat_progress=0,
    participant_fame=800,
    decks_used_today=4,
    decks_used_total=None,
):
    if decks_used_total is None:
        decks_used_total = decks_used_today
    return {
        "name": name,
        "tag": tag,
        "fame": boat_progress,
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


def race_context(period_type="warDay", period_index=12, completed_points=None):
    completed_points = completed_points or {}
    period_logs = []
    for offset, points_by_tag in enumerate(completed_points.values(), start=1):
        period_logs.append(
            {
                "periodIndex": period_index - len(completed_points) + offset - 1,
                "items": [
                    {"clan": {"tag": tag}, "pointsEarned": points}
                    for tag, points in points_by_tag.items()
                ],
            }
        )
    return {
        "periodType": period_type,
        "periodIndex": period_index,
        "periodLogs": period_logs,
    }


def test_regular_river_race_matches_control_site_daily_values():
    clan = race_clan(
        "Brabant Royale",
        "#9YP8UY",
        boat_progress=5552,
        participant_fame=96775,
        decks_used_today=195,
        decks_used_total=575,
    )
    context = race_context(
        completed_points={
            "day1": {"#9YP8UY": 32000},
            "day2": {"#9YP8UY": 32100},
        }
    )

    row = MODULE.build_overview_rows([clan], context)[0]

    assert row["score_scope"] == "river_race_day"
    assert row["score_available"] is True
    assert row["boat_points"] == 5552
    assert row["cumulative_medals"] == 96775
    assert row["medals"] == 32675
    assert row["decks_used_today"] == 195
    assert row["avg_medals_per_deck"] == 167.56
    assert row["projected_medals"] == 33513


def test_regular_average_is_not_affected_by_decks_on_previous_days():
    clans = [
        race_clan("Fewer earlier", "#ONE", 1000, 90000, 195, 450),
        race_clan("More earlier", "#TWO", 2000, 120000, 195, 750),
    ]
    context = race_context(
        completed_points={
            "day1": {"#ONE": 28000, "#TWO": 43000},
            "day2": {"#ONE": 29325, "#TWO": 44325},
        }
    )

    rows = MODULE.build_overview_rows(clans, context)

    assert {row["avg_medals_per_deck"] for row in rows} == {167.56}
    assert {row["medals"] for row in rows} == {32675}


def test_regular_day_one_uses_cumulative_participant_score_without_prior_logs():
    row = MODULE.build_overview_rows(
        [race_clan("Brabant Royale", "#9YP8UY", 300, 1475, 7, 7)],
        race_context(period_index=10),
    )[0]

    assert row["medals"] == 1475
    assert row["avg_medals_per_deck"] == 210.71
    assert row["boat_points"] == 300


def test_regular_score_is_unknown_when_a_required_period_log_is_missing():
    clan = race_clan("Brabant Royale", "#9YP8UY", 5552, 96775, 195, 575)
    context = race_context(
        completed_points={"day2_only": {"#9YP8UY": 32100}}
    )

    row = MODULE.build_overview_rows([clan], context)[0]

    assert row["score_available"] is False
    assert row["medals"] is None
    assert row["avg_medals_per_deck"] is None
    assert row["projected_medals"] is None
    assert row["boat_points"] == 5552

    outlook = MODULE.build_finish_outlook("9YP8UY", [row], [])
    assert outlook["score_available"] is False
    assert outlook["projected_finish"] is None


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
        [race_clan("Brabant Royale", "#9YP8UY", 0, 2400, 4, 12)],
        race_context(period_type="colosseum", period_index=12),
    )
    row = rows[0]

    assert row["score_scope"] == "colosseum_cumulative"
    assert row["medals"] == 2400
    assert row["boat_points"] == 0
    assert row["avg_medals_per_deck"] == 200
    assert row["projection_decks_remaining"] == 396
    assert row["projected_medals"] == 81600

    players = [{"attacks_left_today": 0, "decks_used_today": 4}]
    outlook = MODULE.build_finish_outlook("9YP8UY", rows, players, is_colosseum=True)

    assert outlook["projected_finish"] == 81600
    assert outlook["best_finish"] == 81600
    assert outlook["worst_finish"] == 81600
    assert outlook["projection_scope"] == "colosseum_remaining_days"
