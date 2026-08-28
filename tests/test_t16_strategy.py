from datetime import datetime, timezone
from unittest.mock import Mock, patch

from api import strategy_engine as engine
from war_analytics_metrics import (
    build_strategic_week_metadata,
    collect_analytics_data,
)


NOW = datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc)


def war_phase(**overrides):
    phase = {
        "state": "warDay",
        "periodType": "war",
        "periodIndex": 2,
        "sectionIndex": 3,
        "source": "royaleapi_proxy",
        "clan_tag": "9YP8UY",
        "season_id": 134,
        "race_created_at": "2026-08-28T08:00:00Z",
    }
    phase.update(overrides)
    return phase


def reliable_player(**overrides):
    player = {
        "tag": "#PLAYER1",
        "name": "Alice",
        "role": "Elder",
        "card_depth": 12,
        "observed_war_reliability": {
            "reliability": 95,
            "sample_size": 4,
        },
    }
    player.update(overrides)
    return player


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
        },
    ]


def test_complete_boat_advice_is_manual_and_uses_defense_need():
    context = engine.build_war_context(war_phase(), now=NOW)
    result = engine.build_boat_eligibility_advice(
        [reliable_player()],
        context=context,
        war_need=True,
        target_clan={"id": "opp", "boat_defenses_remaining": 2},
    )

    assert result["status"] == "available"
    assert result["advice_only"] is True
    assert result["automatic_action"] is False
    assert result["action"] is None
    assert result["players"][0]["eligible"] is True
    assert result["players"][0]["card_depth"] == 12
    assert result["players"][0]["observed_war_reliability"] == 95
    assert result["defense"]["boat_defenses_remaining"] == 2


def test_missing_or_partial_defense_is_unknown_and_zero_is_preserved():
    context = engine.build_war_context(war_phase(), now=NOW)
    result = engine.build_boat_eligibility_advice(
        [reliable_player()],
        context=context,
        war_need=True,
        target_clan={"id": "opp", "boat_defenses_today": 0},
    )

    player = result["players"][0]
    assert result["defense"]["boat_defenses_today"] == 0
    assert result["defense"]["boat_defenses_remaining"] is None
    assert result["defense"]["status"] == "partial"
    assert player["eligible"] is None
    assert player["eligibility_status"] == "unknown"
    assert "boat_defenses_remaining" in player["unknown_fields"]


def test_unknown_or_absent_war_need_does_not_recommend_a_player():
    context = engine.build_war_context(war_phase(), now=NOW)
    unknown = engine.build_boat_eligibility_advice(
        [reliable_player()],
        context=context,
        target_clan={"id": "opp"},
    )
    not_needed = engine.build_boat_eligibility_advice(
        [reliable_player()],
        context=context,
        war_need=False,
        target_clan={"id": "opp", "boat_defenses_remaining": 0},
    )
    explicit_but_destroyed = engine.build_boat_eligibility_advice(
        [reliable_player()],
        context=context,
        war_need=True,
        target_clan={"id": "opp", "boat_defenses_remaining": 0},
    )

    assert unknown["status"] == "unknown"
    assert unknown["players"][0]["eligible"] is None
    assert not_needed["status"] == "not_needed"
    assert not_needed["players"][0]["eligible"] is False
    assert not_needed["eligible_player_tags"] == []
    assert explicit_but_destroyed["status"] == "not_needed"
    assert explicit_but_destroyed["players"][0]["eligible"] is False


def test_low_role_or_observed_reliability_is_not_boot_eligible():
    context = engine.build_war_context(war_phase(), now=NOW)
    result = engine.build_boat_eligibility_advice(
        [
            reliable_player(
                role="Recruit",
                observed_war_reliability={"reliability": 65, "sample_size": 4},
            )
        ],
        context=context,
        war_need=True,
        target_clan={"id": "opp", "boat_defenses_remaining": 2},
    )

    player = result["players"][0]
    assert player["eligible"] is False
    assert player["eligibility_status"] == "not_eligible"
    assert len(player["reasons"]) == 2


def test_colosseum_and_stale_phases_have_no_boat_advice():
    colosseum = engine.build_boat_eligibility_advice(
        [reliable_player()],
        context=engine.build_war_context(
            war_phase(periodType="colosseum"),
            now=NOW,
        ),
        war_need=True,
        target_clan={"id": "opp", "boat_defenses_remaining": 2},
    )
    stale = engine.build_boat_eligibility_advice(
        [reliable_player()],
        context=engine.build_war_context(
            war_phase(is_stale=True),
            now=NOW,
        ),
        war_need=True,
        target_clan={"id": "opp", "boat_defenses_remaining": 2},
    )

    assert colosseum["status"] == "not_applicable"
    assert colosseum["recommendation_available"] is False
    assert colosseum["players"][0]["eligibility_status"] == "not_applicable"
    assert "colosseum" in colosseum["reason"].lower()
    assert stale["status"] == "blocked"
    assert stale["players"][0]["eligible"] is None


def test_all_t16_strategy_modes_are_reported_without_changing_legacy_mode():
    our, opponent = strategy_clans()
    for strategy_mode in ("normal", "protect_position", "strategic_experiment"):
        raw_phase = war_phase(strategy_mode=strategy_mode)
        if strategy_mode == "strategic_experiment":
            raw_phase["strategic_week"] = {
                "reason": "Test losse week",
                "actor": "leader-1",
                "race_key": "race-134-3",
                "included_in_normal_analytics": False,
                "observed_outcome": "mixed",
            }
        context = engine.build_war_context(raw_phase, now=NOW)
        projections = engine.build_projections([our, opponent], context["scoreRules"])
        targets = engine.build_rank_targets(
            our,
            [opponent],
            projections,
            context["scoreRules"],
        )
        recommendation = engine.recommend_strategy(
            context,
            our,
            [opponent],
            projections,
            targets,
        )

        assert recommendation["strategy_mode"] == strategy_mode
        assert recommendation["advice_only"] is True
        assert recommendation["automatic_action"] is False
        if strategy_mode == "strategic_experiment":
            assert recommendation["strategic_week"]["metadata_status"] == "complete"
            assert recommendation["experiment_report"]["observed_outcome"] == "mixed"
            assert "geen garantie" in recommendation["experiment_report"]["claim"].lower()


def test_strategic_week_requires_explicit_metadata_and_fails_closed():
    complete = build_strategic_week_metadata(
        war_phase(
            strategy_mode="strategic_experiment",
            strategy_reason="Loose-to-win observatie",
            strategy_actor="leader-1",
            strategy_race_key="race-134-3",
        )
    )
    incomplete = build_strategic_week_metadata(
        war_phase(strategy_mode="strategic_experiment")
    )

    assert complete["metadata_status"] == "complete"
    assert complete["included_in_normal_analytics"] is False
    assert complete["reason"] == "Loose-to-win observatie"
    assert complete["actor"] == "leader-1"
    assert complete["race_key"] == "race-134-3"
    assert incomplete["metadata_status"] == "incomplete"
    assert incomplete["included_in_normal_analytics"] is True
    assert incomplete["reason"] is None
    assert incomplete["race_key"] == "9YP8UY:134:3:2026-08-28T08:00:00Z"
    assert "reason" in incomplete["uncertainties"][0]


def test_t13_leader_decision_shape_drives_experiment_metadata_without_cross_clan_leakage():
    decision = {
        "decision_type": "strategic_experiment",
        "clan_tag": "#9YP8UY",
        "reason": "Loose-to-win observatie",
        "actor": "leader-1",
        "related_race_key": "race-134-3",
        "included_in_normal_analytics": False,
    }
    context = engine.build_war_context(
        war_phase(policy={"leader_decisions": [decision]}),
        now=NOW,
    )

    assert context["strategy_mode"] == "strategic_experiment"
    assert context["strategic_week"]["metadata_status"] == "complete"
    assert context["strategic_week"]["race_key"] == "race-134-3"
    assert context["strategic_week"]["included_in_normal_analytics"] is False

    other_clan = dict(decision, clan_tag="#OTHER")
    unrelated = engine.build_strategic_week_metadata(
        war_phase(policy={"leader_decisions": [other_clan]})
    )
    assert unrelated["strategy_mode"] == "normal"
    assert unrelated["is_strategic_week"] is False


def test_strategy_package_exposes_t16_reports_without_promising_action():
    clans = strategy_clans()
    result = engine.build_strategy_package(
        clans,
        "Brabant Royale",
        [reliable_player()],
        {},
        war_phase(
            strategy_mode="strategic_experiment",
            strategic_week={
                "reason": "Loose-to-win observatie",
                "actor": "leader-1",
                "race_key": "race-134-3",
            },
            boat_need=True,
        ),
        now=NOW,
    )

    assert result["strategy_report"]["strategy_mode"] == "strategic_experiment"
    assert result["boat_eligibility"]["advice_only"] is True
    assert result["boat_eligibility"]["automatic_action"] is False
    assert result["recommendation"]["automatic_action"] is False


def test_analytics_labels_and_excludes_a_complete_strategic_week():
    def response(payload):
        mocked = Mock(status_code=200, content=b"json")
        mocked.json.return_value = payload
        return mocked

    race = {
        "seasonId": 134,
        "createdDate": "20990727T094301.000Z",
        "state": "warDay",
        "periodType": "war",
        "periodIndex": 2,
        "sectionIndex": 3,
        "strategy_mode": "strategic_experiment",
        "strategy_reason": "Loose-to-win observatie",
        "strategy_actor": "leader-1",
        "race_key": "race-134-3",
        "observed_outcome": "mixed",
        "standings": [
            {
                "clan": {
                    "tag": "#9YP8UY",
                    "participants": [
                        {
                            "tag": "#PLAYER1",
                            "name": "Alice",
                            "fame": 2000,
                            "repairPoints": 0,
                            "decksUsed": 16,
                        }
                    ],
                }
            }
        ],
    }

    with (
        patch("war_analytics_metrics.requests.get") as mocked_get,
        patch("war_analytics_metrics.load_history_races_from_env") as mocked_history,
        patch("war_analytics_metrics.load_week_exclusions_from_env") as mocked_exclusions,
        patch.dict("os.environ", {"CLASH_ROYALE_API_KEY": "test-key"}, clear=False),
    ):
        mocked_get.side_effect = [
            response({"items": [{"tag": "#PLAYER1", "name": "Alice", "role": "member"}]}),
            response({"items": [race]}),
            response({}),
        ]
        mocked_history.return_value = ([], {"enabled": False})
        mocked_exclusions.return_value = ([], {"enabled": False})

        payload = collect_analytics_data(clan_tag="9YP8UY")

    metadata = payload["week_metadata"]["134-1"]["strategic_week"]
    assert metadata["metadata_status"] == "complete"
    assert metadata["reason"] == "Loose-to-win observatie"
    assert metadata["actor"] == "leader-1"
    assert metadata["race_key"] == "race-134-3"
    assert metadata["included_in_normal_analytics"] is False
    assert payload["normal_analytics_excluded_weeks"] == ["134-1"]
    assert payload["week_evaluations"]["PLAYER1"]["134-1"]["included"] is False
    assert payload["ratio_scores"][0]["excluded_weeks"] == 1
