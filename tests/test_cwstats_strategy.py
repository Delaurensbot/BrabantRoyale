from Royale_api import ClanOverview
from api.cwstats import build_race_rows, build_strategy, build_war_phase


def clan(name, decks, avg, projected, boat, medals, trophies=4000):
    return ClanOverview(
        name=name,
        decks_used_today=decks,
        decks_total_today=200,
        avg_medals_per_deck=avg,
        projected_medals=projected,
        boat_points=boat,
        current_medals=medals,
        trophies=trophies,
    )


def test_war_phase_prefers_royaleapi_day_over_cwstats_day():
    phase = build_war_phase(4, {"active_day": 1, "is_colosseum_weekend": False})

    assert phase["day"] == 4
    assert phase["source"] == "royaleapi"
    assert phase["label"] == "River Race - Dag 4"


def test_strategy_safe_lead_allows_optional_boat_target():
    rows = build_race_rows([
        clan("Brabant Royale", 120, 180.0, 36000, 9000, 21600),
        clan("Threat Clan", 130, 170.0, 33000, 7000, 22100),
        clan("Third Clan", 110, 150.0, 30000, 3000, 16500),
    ])
    phase = build_war_phase(3, {"active_day": None, "is_colosseum_weekend": False})

    strategy = build_strategy(rows, phase, "Brabant Royale", {})

    assert strategy["risk_level"] == "safe"
    assert strategy["safe_boat_attack_budget"] > 0
    assert strategy["boat_targets"][0]["clan"] == "Threat Clan"


def test_strategy_small_lead_keeps_focus_on_medals():
    rows = build_race_rows([
        clan("Brabant Royale", 150, 160.0, 32000, 8000, 24000),
        clan("Threat Clan", 150, 158.0, 31400, 7600, 23700),
    ])
    phase = build_war_phase(3, {"active_day": None, "is_colosseum_weekend": False})

    strategy = build_strategy(rows, phase, "Brabant Royale", {})

    assert strategy["risk_level"] == "watch"
    assert strategy["safe_boat_attack_budget"] == 0
    assert strategy["boat_targets"] == []


def test_strategy_behind_does_not_suggest_boat_target():
    rows = build_race_rows([
        clan("Leader Clan", 130, 175.0, 35000, 9000, 22750),
        clan("Brabant Royale", 130, 160.0, 32000, 6000, 20800),
    ])
    phase = build_war_phase(4, {"active_day": None, "is_colosseum_weekend": False})

    strategy = build_strategy(rows, phase, "Brabant Royale", {})

    assert strategy["risk_level"] == "behind"
    assert strategy["needed_medals"] == 3001
    assert strategy["boat_targets"] == []


def test_strategy_colosseum_disables_boat_targets():
    rows = build_race_rows([
        clan("Brabant Royale", 120, 180.0, 36000, 9000, 21600),
        clan("Threat Clan", 120, 170.0, 34000, 7000, 20400),
    ])
    phase = build_war_phase(2, {"active_day": None, "is_colosseum_weekend": True})

    strategy = build_strategy(rows, phase, "Brabant Royale", {})

    assert strategy["risk_level"] == "colosseum"
    assert strategy["safe_boat_attack_budget"] == 0
    assert strategy["boat_targets"] == []
