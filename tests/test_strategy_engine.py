from datetime import datetime, timezone

import pytest

from api import strategy_engine as engine


def clan(id_, name, medals, remaining, avg=160, confidence="high", current_rank=None):
    return {
        "id": id_,
        "name": name,
        "currentRank": current_rank,
        "currentMedals": medals,
        "decksRemainingToday": remaining,
        "blendedAveragePerDeck": avg,
        "todayAveragePerDeck": avg,
        "dataConfidence": confidence,
    }


class OverviewClan:
    def __init__(
        self,
        name,
        medals,
        avg,
        projected,
        decks_used=None,
        decks_total=None,
        boat=0,
        trophies=4000,
    ):
        self.name = name
        self.current_medals = medals
        self.avg_medals_per_deck = avg
        self.projected_medals = projected
        self.decks_used_today = decks_used
        self.decks_total_today = decks_total
        self.boat_points = boat
        self.trophies = trophies


def context(mode="river_race", day=4, target_rank=1):
    return engine.build_war_context(
        {"mode": mode, "day": day},
        now=datetime(2026, 6, 28, 8, 0, tzinfo=timezone.utc),
        target_rank=target_rank,
    )


def projections(*rows):
    return engine.build_projections(list(rows), context().get("scoreRules"))


def recommendation(our, opponents, ctx=None):
    ctx = ctx or context()
    all_clans = [our] + list(opponents)
    proj = engine.build_projections(all_clans, ctx.get("scoreRules"))
    targets = engine.build_rank_targets(our, opponents, proj, ctx.get("scoreRules"))
    return engine.recommend_strategy(ctx, our, opponents, proj, targets), targets, proj


def test_first_place_mathematically_safe_all_losses_allowed():
    our = clan("br", "Brabant Royale", 30000, 10)
    opp = clan("opp", "Opponent", 10000, 10)

    rec, _, _ = recommendation(our, [opp])

    assert rec["mode"] == "POSITION_LOCKED_THROW"
    assert rec["actionPlan"]["minimumWins"] == 0
    assert rec["actionPlan"]["maximumSafeLosses"] == 10


def test_last_place_locked_can_lose_everything():
    our = clan("br", "Brabant Royale", 1000, 5)
    opp = clan("opp", "Opponent", 10000, 5)

    rec, _, _ = recommendation(our, [opp], context(target_rank=1))

    assert rec["mode"] == "POSITION_LOCKED_THROW"
    assert rec["bestPossibleRank"] == rec["worstPossibleRank"]
    assert rec["actionPlan"]["maximumSafeLosses"] == 5


def test_expected_first_is_not_mathematically_safe():
    our = clan("br", "Brabant Royale", 10000, 10, avg=180)
    opp = clan("opp", "Opponent", 9800, 10, avg=170)

    rec, _, _ = recommendation(our, [opp])

    assert rec["mode"] != "POSITION_LOCKED_THROW"
    assert rec["worstPossibleRank"] > rec["bestPossibleRank"]


def test_colosseum_has_no_early_finish_advice():
    our = clan("br", "Brabant Royale", 10000, 10, avg=180)
    opp = clan("opp", "Opponent", 9800, 10, avg=170)

    rec, _, _ = recommendation(our, [opp], context(mode="colosseum"))

    assert rec["mode"] == "COLOSSEUM_PUSH"
    assert rec["actionPlan"]["recommendedBoatAttacks"] == 0


def test_colosseum_locked_allows_losses():
    our = clan("br", "Brabant Royale", 30000, 10)
    opp = clan("opp", "Opponent", 10000, 10)

    rec, _, _ = recommendation(our, [opp], context(mode="colosseum"))

    assert rec["mode"] == "COLOSSEUM_LOCKED_THROW"
    assert rec["actionPlan"]["maximumSafeLosses"] == 10


def test_target_rank_one_impossible_finds_alternative():
    our = clan("br", "Brabant Royale", 9000, 10, avg=160)
    opp = clan("opp", "Opponent", 13000, 10, avg=180)
    mid = clan("mid", "Middle Clan", 9500, 10, avg=130)

    rec, targets, _ = recommendation(our, [opp, mid])

    assert targets[0]["status"] == "impossible"
    assert rec["mode"] == "POSITION_UNREACHABLE"


def test_rising_impossible_but_falling_possible_protects_current_position():
    our = clan("br", "Brabant Royale", 10000, 10, avg=160, current_rank=2)
    leader = clan("leader", "Leader Clan", 13000, 10, avg=180, current_rank=1)
    chaser = clan("chaser", "Chaser Clan", 9500, 10, avg=160, current_rank=3)

    rec, _, _ = recommendation(our, [leader, chaser])

    assert rec["mode"] == "PROTECT_CURRENT_POSITION"
    assert rec["targetRank"] == 2
    assert rec["actionPlan"]["minimumWins"] == 6
    assert rec["actionPlan"]["maximumSafeLosses"] == 4


def test_early_finish_context_allows_balancing_losses():
    our = clan("br", "Brabant Royale", 18000, 12, avg=160)
    opp = clan("opp", "Opponent", 17500, 12, avg=160)
    ctx = context(day=3)
    ctx["raceFinished"] = True
    ctx["placementFrozenAfterFinish"] = True

    rec, _, _ = recommendation(our, [opp], ctx)

    assert rec["mode"] == "EARLY_FINISH_BALANCING"
    assert rec["actionPlan"]["maximumSafeLosses"] == 12


def test_missing_deckdata_is_data_incomplete():
    our = clan("br", "Brabant Royale", 10000, None, avg=160)
    opp = clan("opp", "Opponent", 9000, 10, avg=160)

    rec, _, _ = recommendation(our, [opp])

    assert rec["mode"] == "DATA_INCOMPLETE"


def test_null_current_medals_not_treated_as_zero():
    our = clan("br", "Brabant Royale", None, 10, avg=160)
    opp = clan("opp", "Opponent", 9000, 10, avg=160)

    rec, _, _ = recommendation(our, [opp])

    assert rec["mode"] == "DATA_INCOMPLETE"
    assert "currentMedals" in rec["summary"]


def test_decks_used_above_capacity_is_marked_unknown():
    class C:
        name = "Brabant Royale"
        current_medals = 10000
        decks_used_today = 220
        decks_total_today = 200
        avg_medals_per_deck = 160
        projected_medals = 10000
        boat_points = 0
        trophies = 4000

    rows = engine.normalize_clans([C], "Brabant Royale")

    assert rows[0]["decksRemainingToday"] is None
    assert rows[0]["warnings"]


def test_current_production_fixture_infers_decks_and_does_not_lock_position():
    clans = [
        OverviewClan("Brabant Royale", 21800, 156.83, 31366, boat=9105),
        OverviewClan("Clash Bros 2", 20100, 176.32, 35264, boat=3612),
        OverviewClan("Deja Vu", 19550, 168.53, 33706, boat=3105),
        OverviewClan("los mancos", 19350, 159.92, 31983, boat=5305),
        OverviewClan("Demons Rebirth", 17800, 160.36, 32072, boat=5705),
    ]

    result = engine.build_strategy_package(
        clans,
        "Brabant Royale",
        players=[],
        finish_outlook={},
        war_phase={"mode": "river_race", "day": 4},
        now=datetime(2026, 6, 28, 8, 0, tzinfo=timezone.utc),
    )

    rows = {row["id"]: row for row in result["raceRows"]}
    assert rows["brabantroyale"]["decksUsedToday"] == 139
    assert rows["clashbros2"]["decksUsedToday"] == 114
    assert rows["dejavu"]["decksUsedToday"] == 116
    assert rows["losmancos"]["decksUsedToday"] == 121
    assert rows["demonsrebirth"]["decksUsedToday"] == 111

    projections_by_id = {row["clanId"]: row for row in result["projections"]}
    assert projections_by_id["brabantroyale"]["floorFinal"] == 27900
    assert projections_by_id["brabantroyale"]["expectedFinal"] == 31366
    assert projections_by_id["brabantroyale"]["ceilingFinal"] == 34000
    assert projections_by_id["clashbros2"]["floorFinal"] == 28700
    assert projections_by_id["clashbros2"]["expectedFinal"] == 35264
    assert projections_by_id["clashbros2"]["ceilingFinal"] == 37300

    rec = result["recommendation"]
    assert rec["bestPossibleRank"] == 1
    assert rec["worstPossibleRank"] == 5
    assert rec["currentRankLocked"] is False
    assert rec["mode"] != "POSITION_LOCKED_THROW"
    assert rec["actionPlan"]["maximumSafeLosses"] != 61


def test_unknown_opponent_decks_are_inferred_instead_of_projected_as_zero():
    clans = [
        OverviewClan("Brabant Royale", 21800, 156.83, 31366),
        OverviewClan("Clash Bros 2", 20100, 176.32, 35264),
    ]

    result = engine.build_strategy_package(
        clans,
        "Brabant Royale",
        players=[],
        finish_outlook={},
        war_phase={"mode": "river_race", "day": 4},
        now=datetime(2026, 6, 28, 8, 0, tzinfo=timezone.utc),
    )

    clash = next(row for row in result["raceRows"] if row["id"] == "clashbros2")
    projection = next(row for row in result["projections"] if row["clanId"] == "clashbros2")

    assert clash["decksUsedToday"] == 114
    assert clash["decksRemainingToday"] == 86
    assert projection["floorFinal"] == 28700
    assert projection["expectedFinal"] == 35264
    assert projection["ceilingFinal"] == 37300


def test_fully_unknown_opponent_data_fails_closed():
    our = clan("br", "Brabant Royale", 21800, 61, avg=156.83)
    opponent = clan("opp", "Opponent", 20100, None, avg=None)

    rec, targets, _ = recommendation(our, [opponent])

    assert rec["mode"] == "DATA_INCOMPLETE"
    assert rec["mode"] != "POSITION_LOCKED_THROW"
    assert rec["currentRankLocked"] is False
    assert targets == []


def test_real_first_place_rank_lock_requires_our_floor_above_opponent_ceilings():
    our = clan("br", "Brabant Royale", 30000, 10, avg=160)
    opponents = [
        clan("opp1", "Opponent 1", 10000, 10, avg=160),
        clan("opp2", "Opponent 2", 9000, 10, avg=160),
    ]

    rec, _, _ = recommendation(our, opponents)

    assert rec["mode"] == "POSITION_LOCKED_THROW"
    assert rec["currentRankLocked"] is True
    assert rec["bestPossibleRank"] == 1
    assert rec["worstPossibleRank"] == 1


def test_fixture_target_ladder_uses_projected_opponents_with_tie_margin():
    clans = [
        OverviewClan("Brabant Royale", 21800, 156.83, 31366),
        OverviewClan("Clash Bros 2", 20100, 176.32, 35264),
        OverviewClan("Deja Vu", 19550, 168.53, 33706),
        OverviewClan("los mancos", 19350, 159.92, 31983),
        OverviewClan("Demons Rebirth", 17800, 160.36, 32072),
    ]
    result = engine.build_strategy_package(
        clans,
        "Brabant Royale",
        players=[],
        finish_outlook={},
        war_phase={"mode": "river_race", "day": 4},
        now=datetime(2026, 6, 28, 8, 0, tzinfo=timezone.utc),
    )

    by_rank = {target["rank"]: target for target in result["rankTargets"]}

    assert by_rank[1]["targetScore"] == 35265
    assert by_rank[1]["requiredAveragePerDeck"] == pytest.approx(220.74, abs=0.01)
    assert by_rank[1]["status"] == "impossible"

    assert by_rank[2]["targetScore"] == 33707
    assert by_rank[2]["requiredAveragePerDeck"] == pytest.approx(195.20, abs=0.01)
    assert by_rank[2]["minimumWinsNeeded"] == 59

    assert by_rank[3]["targetScore"] == 32073
    assert by_rank[3]["requiredAveragePerDeck"] == pytest.approx(168.41, abs=0.01)
    assert by_rank[3]["minimumWinsNeeded"] == 42

    assert by_rank[4]["targetScore"] == 31984
    assert by_rank[4]["requiredAveragePerDeck"] == pytest.approx(166.95, abs=0.01)
    assert by_rank[4]["minimumWinsNeeded"] == 41


def test_boat_attack_positive_net_value():
    our = clan("br", "Brabant Royale", 10000, 20)
    target = clan("opp", "Opponent", 9800, 20)

    result = engine.evaluate_boat_strike(context(day=2), our, target, 160, 80, 3, 700)

    assert result["recommend"] is True
    assert result["netStrategicValue"] > 0


def test_boat_attack_low_or_missing_completion_is_not_recommended():
    our = clan("br", "Brabant Royale", 10000, 20)
    target = clan("opp", "Opponent", 9800, 20)

    result = engine.evaluate_boat_strike(context(day=2), our, target, 160, 80, None, 700)

    assert result["recommend"] is False


def test_boat_attack_day_four_without_direct_effect_is_not_recommended():
    our = clan("br", "Brabant Royale", 10000, 20)
    target = clan("opp", "Opponent", 9800, 20)

    result = engine.evaluate_boat_strike(context(day=4), our, target, 160, 80, 3, 0)

    assert result["recommend"] is False


def test_screenshot_fixture_builds_target_ladder_not_single_status():
    our = clan("br", "Brabant Royale", 20750, 68, avg=157.2)
    opponents = [
        clan("clash", "Clash Bros 2", 20100, 68, avg=176.32),
        clan("dejavu", "Deja Vu", 19550, 68, avg=168.53),
        clan("los", "los mancos", 17850, 68, avg=157.96),
        clan("demons", "Demons Rebirth", 17250, 68, avg=161.21),
    ]
    proj = [
        {"clanId": "br", "floorFinal": 27550, "expectedFinal": 31439, "optimisticFinal": 31439, "ceilingFinal": 34350, "expectedRemainingDecks": 68},
        {"clanId": "clash", "floorFinal": 26900, "expectedFinal": 35264, "optimisticFinal": 35264, "ceilingFinal": 33700, "expectedRemainingDecks": 68},
        {"clanId": "dejavu", "floorFinal": 26350, "expectedFinal": 33706, "optimisticFinal": 33706, "ceilingFinal": 33150, "expectedRemainingDecks": 68},
        {"clanId": "los", "floorFinal": 24650, "expectedFinal": 31592, "optimisticFinal": 31592, "ceilingFinal": 31450, "expectedRemainingDecks": 68},
        {"clanId": "demons", "floorFinal": 24050, "expectedFinal": 32242, "optimisticFinal": 32242, "ceilingFinal": 30850, "expectedRemainingDecks": 68},
    ]
    targets = engine.build_rank_targets(our, opponents, proj, context().get("scoreRules"))
    rec = engine.recommend_strategy(context(), our, opponents, proj, targets)

    assert rec["mode"] == "POSITION_UNREACHABLE"
    assert targets[0]["requiredAveragePerDeck"] > 200
    assert targets[1]["requiredAveragePerDeck"] == 190.54
    assert any(target["status"] != "impossible" for target in targets)
