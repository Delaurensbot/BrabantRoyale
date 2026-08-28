from api.cwstats import build_war_phase
from api.reporting import parse_day_label, parse_day_number


def test_official_race_context_never_uses_external_page_labels():
    context = {"period_index": 4, "period_type": "war"}

    assert parse_day_number(context) == 4
    assert parse_day_label(context) == "Day 4"


def test_war_phase_uses_official_day_and_colosseum_metadata():
    phase = build_war_phase(4, {"active_day": None, "is_colosseum_weekend": True})

    assert phase["day"] == 4
    assert phase["source"] == "royaleapi"
    assert phase["mode"] == "colosseum"
    assert phase["label"] == "Colosseum - Dag 4"
