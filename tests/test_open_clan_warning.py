import pytest
import requests
from threading import Barrier

from test_official_race_projection import MODULE, race_clan


@pytest.mark.parametrize("period_type,period,expected", [
    ("training", 0, False), ("training", 1, False), ("training", 2, True),
    ("training", 9, True), ("training", 23, True), ("training", 30, True),
    ("training", None, False), ("training", -5, False),
    ("training", "23", False), ("training", 3, False),
    ("warDay", 3, True), ("warDay", 6, True),
    ("colosseum", 24, True), ("COLOSSEUM", None, True),
    ("unknown", 23, False), (None, 24, False),
])
def test_warning_window_uses_official_phase_and_training_day(period_type, period, expected):
    assert MODULE.open_clan_warning_period({"periodType": period_type, "periodIndex": period}) is expected


@pytest.mark.parametrize("clan_type,expected", [
    ("open", True), ("closed", False), ("inviteOnly", False),
    ("OPEN", True), ("newType", None), (None, None),
])
def test_clan_types_remain_tri_state(clan_type, expected):
    status = MODULE.clan_access_status({"tag": "#AAA", "type": clan_type}, "AAA")
    assert status["is_open"] is expected
    assert status["source"] == ("unknown" if expected is None else "official_api")


def test_wrong_clan_or_malformed_profile_never_creates_warning():
    for profile in [{"tag": "#OTHER", "type": "open"}, {"type": "open"}, [], None]:
        assert MODULE.clan_access_status(profile, "AAA") == {
            "type": None, "is_open": None, "source": "unknown"
        }


def test_optional_profiles_reuse_own_data_and_isolate_failures(monkeypatch):
    rows = MODULE.build_overview_rows([
        race_clan("Ours", "#9YP8UY"), race_clan("Open", "#AAA"),
        race_clan("Closed", "#BBB"), race_clan("Timeout", "#CCC"),
        race_clan("Bad JSON", "#DDD"),
    ], {"periodType": "training", "periodIndex": 23})
    calls = []
    simultaneous = Barrier(4)

    def get_profile(endpoint, key, timeout):
        assert key == "test-only"
        assert timeout == MODULE.CLAN_ACCESS_TIMEOUT
        calls.append(endpoint)
        simultaneous.wait(timeout=3)
        candidate = endpoint.rsplit("%23", 1)[-1]
        if candidate == "CCC":
            raise requests.Timeout("test timeout")
        if candidate == "DDD":
            raise ValueError("test malformed JSON")
        return {"tag": f"#{candidate}", "type": "open" if candidate == "AAA" else "closed"}

    monkeypatch.setattr(MODULE, "request_json", get_profile)
    result = MODULE.with_clan_access_status(
        rows, "9YP8UY", {"tag": "#9YP8UY", "type": "inviteOnly"},
        {"periodType": "training", "periodIndex": 23}, "test-only",
    )
    assert len(calls) == 4
    by_tag = {row["tag"]: row["clan_access"] for row in result}
    assert by_tag["#9YP8UY"]["type"] == "inviteOnly"
    assert by_tag["#AAA"]["is_open"] is True
    assert by_tag["#BBB"]["is_open"] is False
    assert by_tag["#CCC"]["is_open"] is None
    assert by_tag["#DDD"]["is_open"] is None
    assert [{key: value for key, value in row.items() if key != "clan_access"} for row in result] == rows
    assert all("clan_access" not in row for row in rows), "Original scoring rows must not mutate"


def test_early_training_skips_opponent_requests_and_missing_is_unknown(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("No additional profile requests during training days 1 and 2")

    monkeypatch.setattr(MODULE, "request_json", unexpected)
    rows = [{"tag": "#9YP8UY"}, {"tag": "#AAA"}]
    for period in [0, 1, 21, 22]:
        result = MODULE.with_clan_access_status(
            rows, "9YP8UY", {"tag": "#9YP8UY", "type": "open"},
            {"periodType": "training", "periodIndex": period}, "test-only",
        )
        assert result[0]["clan_access"]["is_open"] is True
        assert result[1]["clan_access"]["is_open"] is None


def test_lookup_is_fresh_on_refresh_and_deduplicates_tags(monkeypatch):
    clan_type = "open"
    calls = []

    def get_profile(endpoint, key, timeout):
        calls.append(endpoint)
        return {"tag": "#AAA", "type": clan_type}

    monkeypatch.setattr(MODULE, "request_json", get_profile)
    rows = [{"tag": "#AAA"}, {"tag": "#AAA"}]
    race = {"periodType": "colosseum"}
    assert MODULE.with_clan_access_status(rows, "9YP8UY", {}, race, "test-only")[0]["clan_access"]["is_open"] is True
    clan_type = "closed"
    assert MODULE.with_clan_access_status(rows, "9YP8UY", {}, race, "test-only")[0]["clan_access"]["is_open"] is False
    assert len(calls) == 2


def test_endpoint_still_returns_racedata_when_an_opponent_lookup_fails(monkeypatch):
    own = {"tag": "#9YP8UY", "name": "Brabant Royale", "type": "inviteOnly"}
    race = {"periodType": "warDay", "periodIndex": 24, "sectionIndex": 3,
            "clans": [race_clan("Ours", "#9YP8UY"), race_clan("Opponent", "#AAA")],
            "clan": {"participants": []}}

    def get_data(endpoint, key, timeout=20):
        if endpoint.endswith("/currentriverrace"):
            return race
        if endpoint.endswith("/members"):
            return {"items": []}
        if endpoint.endswith("%239YP8UY"):
            return own
        assert timeout == MODULE.CLAN_ACCESS_TIMEOUT
        raise requests.HTTPError("test upstream failure")

    monkeypatch.setenv("CLASH_ROYALE_API_KEY", "test-only-not-a-real-key")
    monkeypatch.setattr(MODULE, "request_json", get_data)
    captured = {}
    instance = MODULE.handler.__new__(MODULE.handler)
    instance.path = "/api/test-clan-prototype?clan=9YP8UY"
    instance._send_json = lambda status, payload: captured.update(status=status, payload=payload)
    instance.do_GET()
    assert captured["status"] == 200
    payload = captured["payload"]
    assert payload["ok"] is True
    assert len(payload["overview_rows"]) == 2
    assert payload["overview_rows"][1]["clan_access"]["is_open"] is None
    assert [{key: value for key, value in row.items() if key != "clan_access"}
            for row in payload["overview_rows"]] == MODULE.build_overview_rows(race["clans"], race)
    assert payload["finish_outlook"]["score_available"] is True
