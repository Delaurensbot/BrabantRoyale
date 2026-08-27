from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from api import strategy_engine as engine
from war_analytics_metrics import (
    _race_phase_metadata,
    collect_analytics_data,
    format_average_contribution,
    row_total_for_weeks,
)


NOW = datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc)


def strategy_clans():
    return [
        {
            "id": "br",
            "name": "Brabant Royale",
            "currentRank": 1,
            "currentMedals": 10000,
            "decksRemainingToday": 10,
            "blendedAveragePerDeck": 180,
            "todayAveragePerDeck": 180,
            "dataConfidence": "high",
        },
        {
            "id": "opp",
            "name": "Opponent",
            "currentRank": 2,
            "currentMedals": 9000,
            "decksRemainingToday": 10,
            "blendedAveragePerDeck": 170,
            "todayAveragePerDeck": 170,
            "dataConfidence": "high",
        },
    ]


def recommendation_for(phase):
    context = engine.build_war_context(phase, now=NOW)
    clans = strategy_clans()
    projections = engine.build_projections(clans, context["scoreRules"])
    targets = engine.build_rank_targets(
        clans[0], clans[1:], projections, context["scoreRules"]
    )
    recommendation = engine.recommend_strategy(
        context,
        clans[0],
        clans[1:],
        projections,
        targets,
    )
    return context, recommendation


def test_official_war_day_context_drives_strategy_and_keeps_fields():
    context, recommendation = recommendation_for(
        {
            "state": "warDay",
            "periodType": "war",
            "periodIndex": 2,
            "sectionIndex": 3,
            "finishTime": "2026-08-27T12:00:00Z",
            "source": "royaleapi_proxy",
        }
    )

    assert context["phaseStatus"] == "war_day"
    assert context["phaseDataQuality"] == "official"
    assert context["periodType"] == "war"
    assert context["periodIndex"] == 2
    assert context["sectionIndex"] == 3
    assert context["finishTime"] == "2026-08-27T12:00:00Z"
    assert recommendation["phaseStatus"] == "war_day"
    assert recommendation["strategyAvailable"] is True


def test_training_is_distinct_and_has_no_strategy_or_boat_advice():
    context, recommendation = recommendation_for(
        {
            "state": "training",
            "periodType": "training",
            "periodIndex": 2,
            "sectionIndex": 1,
        }
    )

    assert context["phaseStatus"] == "training"
    assert recommendation["strategyAvailable"] is False
    assert recommendation["actionPlan"]["minimumWins"] is None
    boat = engine.evaluate_boat_strike(
        context,
        strategy_clans()[0],
        strategy_clans()[1],
        180,
        80,
        1,
        700,
    )
    assert boat["recommend"] is False
    assert "training" in boat["reason"].lower()


def test_colosseum_has_no_boat_recommendation_even_with_positive_numbers():
    context, recommendation = recommendation_for(
        {
            "state": "warDay",
            "periodType": "colosseum",
            "periodIndex": 2,
            "sectionIndex": 1,
        }
    )

    assert context["phaseStatus"] == "colosseum"
    assert recommendation["phaseStatus"] == "colosseum"
    assert recommendation["actionPlan"]["recommendedBoatAttacks"] == 0
    boat = engine.evaluate_boat_strike(
        context,
        strategy_clans()[0],
        strategy_clans()[1],
        180,
        80,
        1,
        700,
    )
    assert boat["recommend"] is False
    assert "colosseum" in boat["reason"].lower()


def test_finished_context_uses_finish_time_and_disables_strategy():
    context, recommendation = recommendation_for(
        {
            "state": "war",
            "periodType": "war",
            "periodIndex": 2,
            "sectionIndex": 3,
            "finishTime": "2026-08-27T07:00:00Z",
        }
    )

    assert context["phaseStatus"] == "finished"
    assert context["raceFinished"] is True
    assert recommendation["strategyAvailable"] is False
    assert recommendation["actionPlan"]["maximumSafeLosses"] is None


def test_missing_context_fails_closed_without_zero_work_plan():
    context, recommendation = recommendation_for({})

    assert context["phaseStatus"] == "not_available"
    assert recommendation["mode"] == "DATA_INCOMPLETE"
    assert recommendation["strategyAvailable"] is False
    assert recommendation["actionPlan"] == {
        "minimumWins": None,
        "maximumSafeLosses": None,
        "recommendedBoatAttacks": 0,
        "decksToHoldTemporarily": None,
    }
    boat = engine.evaluate_boat_strike(
        context,
        strategy_clans()[0],
        strategy_clans()[1],
        180,
        80,
        1,
        700,
    )
    assert boat["recommend"] is False


def test_stale_context_is_not_treated_as_current_war_day():
    context, recommendation = recommendation_for(
        {
            "state": "warDay",
            "periodType": "war",
            "periodIndex": 2,
            "sectionIndex": 3,
            "is_stale": True,
            "stale_reason": "upstream_server_error",
        }
    )

    assert context["phaseStatus"] == "stale"
    assert context["dataStatus"] == "stale"
    assert recommendation["strategyAvailable"] is False
    assert recommendation["bestPossibleRank"] is None


def test_error_context_is_not_treated_as_current_war_day():
    context, recommendation = recommendation_for(
        {
            "data_status": "error",
            "error_code": "forbidden",
        }
    )

    assert context["phaseStatus"] == "error"
    assert context["dataStatus"] == "error"
    assert recommendation["strategyAvailable"] is False
    assert recommendation["actionPlan"]["minimumWins"] is None


def test_derived_projections_and_capacity_are_explicitly_estimated():
    def clan_type(name, medals, decks_used, projected):
        return SimpleNamespace(
            name=name,
            current_medals=medals,
            decks_used_today=decks_used,
            decks_total_today=None,
            avg_medals_per_deck=160,
            projected_medals=projected,
            boat_points=None,
            trophies=None,
        )
    result = engine.build_strategy_package(
        [
            clan_type("Brabant Royale", 10000, 10, None),
            clan_type("Opponent", 9000, 10, None),
        ],
        "Brabant Royale",
        players=[],
        finish_outlook={},
        war_phase={
            "state": "warDay",
            "periodType": "war",
            "periodIndex": 2,
            "sectionIndex": 3,
        },
        now=NOW,
    )

    own = next(row for row in result["raceRows"] if row["id"] == "brabantroyale")
    own_projection = next(
        projection
        for projection in result["projections"]
        if projection["clanId"] == "brabantroyale"
    )
    assert own["estimatedDeckCapacityToday"] == 200
    assert "estimatedDeckCapacityToday" in own["estimatedFields"]
    assert own["valueStatus"] == "estimated"
    assert own_projection["estimated"] is True
    assert own_projection["valueStatus"] == "estimated"
    assert "expectedFinal" in own_projection["estimatedFields"]
    assert result["dataQuality"]["estimatedFields"]


def test_analytics_metadata_preserves_official_phase_fields():
    metadata = _race_phase_metadata(
        {
            "state": "warDay",
            "periodType": "war",
            "periodIndex": 2,
            "sectionIndex": 3,
            "finishTime": "2099-08-27T12:00:00Z",
        }
    )

    assert metadata["phase_status"] == "war_day"
    assert metadata["periodType"] == "war"
    assert metadata["periodIndex"] == 2
    assert metadata["sectionIndex"] == 3
    assert metadata["finishTime"] == "2099-08-27T12:00:00Z"


def test_analytics_missing_values_remain_unknown_instead_of_zero():
    assert row_total_for_weeks({"134-1": None}, ["134-1"]) is None
    assert format_average_contribution({"134-1": None}, ["134-1"]) == "Onbekend"


def test_analytics_collection_does_not_turn_missing_participant_metrics_into_zero():
    def response(payload):
        mocked = Mock(status_code=200, content=b"json")
        mocked.json.return_value = payload
        return mocked

    with (
        patch("war_analytics_metrics.requests.get") as mocked_get,
        patch("war_analytics_metrics.load_history_races_from_env") as mocked_history,
        patch("war_analytics_metrics.load_week_exclusions_from_env") as mocked_exclusions,
        patch.dict("os.environ", {"CLASH_ROYALE_API_KEY": "test-key"}, clear=False),
    ):
        mocked_get.side_effect = [
            response({"items": [{"tag": "#PLAYER1", "name": "Alice", "role": "member"}]}),
            response(
                {
                    "items": [
                        {
                            "seasonId": 134,
                            "createdDate": "20990727T094301.000Z",
                            "state": "warDay",
                            "periodType": "war",
                            "periodIndex": 2,
                            "sectionIndex": 3,
                            "clans": [
                                {
                                    "tag": "#9YP8UY",
                                    "participants": [{"tag": "#PLAYER1", "name": "Alice"}],
                                }
                            ],
                        }
                    ]
                }
            ),
            response({}),
        ]
        mocked_history.return_value = ([], {"enabled": False})
        mocked_exclusions.return_value = ([], {"enabled": False})

        payload = collect_analytics_data(clan_tag="9YP8UY")

    row = payload["contribution_table"]["rows"][0]
    assert row[2] == "Onbekend"
    assert row[3] == "Onbekend"
    assert payload["ratio_scores"][0]["reliability_score"] is None
    assert payload["week_metadata"]["134-1"]["phase_status"] == "war_day"
