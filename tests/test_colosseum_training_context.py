from datetime import date

from test_official_race_projection import MODULE, race_clan


def context(section, period, today):
    return MODULE.build_week_context(
        {"periodType": "training", "sectionIndex": section, "periodIndex": period},
        today,
    )


def test_current_four_week_season_prepares_colosseum_during_training():
    result = context(3, 23, date(2026, 9, 30))
    assert result == {
        "mode": "colosseum",
        "source": "official_api_indices_and_season_calendar",
        "week": 4,
        "season_weeks": 4,
    }


def test_five_week_season_does_not_mistake_week_four_for_colosseum():
    assert context(3, 23, date(2026, 11, 25))["mode"] == "river_race"
    result = context(4, 30, date(2026, 12, 2))
    assert result["mode"] == "colosseum"
    assert result["season_weeks"] == 5


def test_year_boundary_and_first_monday_season_rollover():
    assert context(3, 23, date(2026, 12, 30))["mode"] == "colosseum"
    assert context(0, 0, date(2027, 1, 4))["mode"] == "river_race"
    assert context(3, 23, date(2027, 1, 4))["mode"] is None


def test_missing_inconsistent_or_stale_indices_stay_unknown():
    for race in [
        {},
        {"sectionIndex": 3},
        {"sectionIndex": 3, "periodIndex": 16},
        {"sectionIndex": 3, "periodIndex": 24},
        {"sectionIndex": 2, "periodIndex": 16},
        {"sectionIndex": 3.5, "periodIndex": 23},
        {"sectionIndex": True, "periodIndex": 23},
    ]:
        result = MODULE.build_week_context(
            {"periodType": "training", **race}, date(2026, 9, 30)
        )
        assert result == {"mode": None, "source": "unknown"}


def test_week_theme_never_activates_colosseum_scoring():
    race = {"periodType": "training", "sectionIndex": 3, "periodIndex": 23}
    assert MODULE.build_week_context(race, date(2026, 9, 30))["mode"] == "colosseum"
    assert MODULE.is_colosseum_race(race) is False
    row = MODULE.build_overview_rows([race_clan("Brabant Royale", "#9YP8UY")], race)[0]
    assert row["score_scope"] == "river_race_day"
    assert row["score_available"] is False
    assert row["medals"] is None
    assert row["projected_medals"] is None


def test_active_colosseum_remains_official_and_unknown_phase_is_not_guessed():
    assert MODULE.build_week_context({"periodType": "COLOSSEUM"}) == {
        "mode": "colosseum", "source": "official_api"
    }
    assert MODULE.build_week_context({"periodType": "warDay", "sectionIndex": 3})["mode"] is None


def test_endpoint_adds_preparation_context_without_changing_existing_fields(monkeypatch):
    race = {
        "periodType": "training", "sectionIndex": 3, "periodIndex": 23,
        "clans": [race_clan("Brabant Royale", "#9YP8UY")],
        "clan": {"participants": []},
    }
    responses = iter([
        {"name": "Brabant Royale", "tag": "#9YP8UY"},
        {"items": []},
        race,
    ])
    monkeypatch.setenv("CLASH_ROYALE_API_KEY", "test-only-not-a-real-key")
    monkeypatch.setattr(MODULE, "request_json", lambda *_: next(responses))
    build_context = MODULE.build_week_context
    monkeypatch.setattr(MODULE, "build_week_context", lambda data: build_context(data, date(2026, 9, 30)))
    captured = {}
    instance = MODULE.handler.__new__(MODULE.handler)
    instance.path = "/api/test-clan-prototype?clan=9YP8UY"
    instance._send_json = lambda status, payload: captured.update(status=status, payload=payload)
    instance.do_GET()

    assert captured["status"] == 200
    payload = captured["payload"]
    assert payload["week_context"]["mode"] == "colosseum"
    assert payload["race_state"]["period_type"] == "training"
    assert payload["race_state"]["is_colosseum_weekend"] is False
    assert payload["race_state"]["battle_day"] is None
    assert payload["overview_rows"] == MODULE.build_overview_rows(race["clans"], race)
    assert payload["finish_outlook"]["projection_scope"] == "river_race_day"
