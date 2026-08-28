import json

import pytest

from api.duel_first import (
    STATUS_API_STALE,
    STATUS_DUEL_FIRST_LIKELY,
    STATUS_EXEMPT,
    STATUS_NOT_STARTED,
    STATUS_SOLO_START_OBSERVED,
    STATUS_UNKNOWN_START,
    DuelFirstValidationError,
    build_race_day_key,
    build_race_key,
    observe_duel_first,
)


RACE_CREATED_AT = "2025-01-01T12:00:00Z"
OBSERVED_AT = "2025-01-01T12:05:00+00:00"
CLAN_TAG = "#9yp8uy"
PLAYER_TAG = "%23player1"
DAY_KEY = build_race_day_key(
    CLAN_TAG,
    128,
    3,
    RACE_CREATED_AT,
    2,
)


def observe(previous, current, **overrides):
    values = {
        "clan_tag": CLAN_TAG,
        "season_id": 128,
        "section_index": 3,
        "race_created_at": RACE_CREATED_AT,
        "period_index": 2,
        "player_tag": PLAYER_TAG,
        "current_decks_used_today": current,
        "previous_decks_used_today": previous,
        "previous_race_day_key": DAY_KEY,
        "observed_at": OBSERVED_AT,
    }
    values.update(overrides)
    return observe_duel_first(**values)


def test_race_and_race_day_keys_include_every_identity_component():
    race_key = build_race_key(
        "#9yp8uy",
        "128",
        "3",
        "20250101T120000.000Z",
    )
    assert race_key == "9YP8UY|128|3|2025-01-01T12:00:00Z"
    assert (
        build_race_day_key(
            "9YP8UY",
            128,
            3,
            "2025-01-01T13:00:00+01:00",
            "2",
        )
        == f"{race_key}|2"
    )


def test_zero_to_one_is_a_solo_start_observation_and_creates_one_event():
    result = observe(0, 1)

    assert result.status == STATUS_SOLO_START_OBSERVED
    assert result.confidence == "high"
    assert "observed" in result.reason.lower()
    assert result.event_key
    assert result.event_identity["event_type"] == STATUS_SOLO_START_OBSERVED
    assert result.new_event is True
    assert result.event["observed_decks_used_today"] == 1
    assert result.event["event_key"] == result.event_key


def test_zero_to_two_is_likely_duel_first_and_creates_one_event():
    result = observe(0, 2)

    assert result.status == STATUS_DUEL_FIRST_LIKELY
    assert result.confidence == "medium"
    assert "likely" in result.reason.lower()
    assert result.event["event_type"] == STATUS_DUEL_FIRST_LIKELY
    assert result.event["details"]["signal"] == "likely"


def test_zero_to_three_is_likely_duel_first():
    result = observe(0, 3)

    assert result.status == STATUS_DUEL_FIRST_LIKELY
    assert result.current_decks_used_today == 3
    assert result.new_event is True


def test_first_measurement_greater_than_zero_is_unknown_start():
    result = observe(None, 2, previous_race_day_key=None)

    assert result.status == STATUS_UNKNOWN_START
    assert result.confidence == "low"
    assert result.event_key is None
    assert result.event is None
    assert "first" in result.reason.lower()


def test_first_measurement_zero_is_not_started():
    result = observe(None, 0, previous_race_day_key=None)

    assert result.status == STATUS_NOT_STARTED
    assert result.confidence == "high"
    assert result.event is None


def test_missing_counter_is_unknown_and_is_not_zero_filled():
    result = observe(0, None)

    assert result.status == STATUS_UNKNOWN_START
    assert result.current_decks_used_today is None
    assert result.event is None
    assert "not treated as zero" in result.reason


def test_new_race_day_ignores_previous_day_counter_and_resets_start_status():
    previous_day_key = build_race_day_key(
        CLAN_TAG,
        128,
        3,
        RACE_CREATED_AT,
        1,
    )
    result = observe(
        0,
        2,
        period_index=2,
        previous_race_day_key=previous_day_key,
    )

    assert result.is_new_race_day is True
    assert result.status == STATUS_UNKNOWN_START
    assert result.event is None
    assert "previous day counter was ignored" in result.reason


def test_same_player_in_different_clans_has_separate_race_and_event_identity():
    first = observe(0, 2)
    second_day_key = build_race_day_key(
        "#differentclan",
        128,
        3,
        RACE_CREATED_AT,
        2,
    )
    second = observe(
        0,
        2,
        clan_tag="#differentclan",
        previous_race_day_key=second_day_key,
    )

    assert first.player_tag == second.player_tag == "PLAYER1"
    assert first.race_key != second.race_key
    assert first.race_day_key != second.race_day_key
    assert first.event_key != second.event_key
    assert first.event_identity["clan_tag"] != second.event_identity["clan_tag"]


def test_exempt_is_explicit_and_suppresses_start_event_even_with_missing_counter():
    result = observe(None, None, exempt=True)

    assert result.status == STATUS_EXEMPT
    assert result.confidence == "high"
    assert result.event_key is None
    assert result.event is None
    assert "explicitly" in result.reason


def test_api_stale_is_explicit_non_accusatory_and_suppresses_event():
    result = observe(0, 2, api_stale=True)

    assert result.status == STATUS_API_STALE
    assert result.confidence == "unknown"
    assert result.event_key is None
    assert result.event is None
    assert "stale" in result.reason.lower()
    assert "likely" not in result.reason.lower()


def test_duplicate_event_key_does_not_return_a_second_event():
    first = observe(0, 2)
    duplicate = observe(0, 2, existing_event_keys=(first.event_key,))

    assert duplicate.status == first.status == STATUS_DUEL_FIRST_LIKELY
    assert duplicate.event_key == first.event_key
    assert duplicate.event_exists is True
    assert duplicate.new_event is False
    assert duplicate.event is None


def test_non_monotone_counter_fails_closed():
    with pytest.raises(DuelFirstValidationError, match="must not decrease"):
        observe(2, 1)


@pytest.mark.parametrize(
    "overrides",
    [
        {"clan_tag": "#bad/tag"},
        {"season_id": None},
        {"section_index": None},
        {"period_index": -1},
        {"race_created_at": "not-a-timestamp"},
        {"player_tag": ""},
        {"previous_race_day_key": "not-a-race-day-key"},
    ],
)
def test_invalid_identity_fails_safely(overrides):
    with pytest.raises(DuelFirstValidationError):
        observe(0, 1, **overrides)


@pytest.mark.parametrize("counter", [True, 1.5, -1, "not-a-counter"])
def test_invalid_counter_fails_safely(counter):
    with pytest.raises(DuelFirstValidationError):
        observe(0, counter)


def test_result_is_serializable_and_uses_observed_or_likely_language():
    result = observe(0, 2)
    encoded = json.dumps(result.as_dict(), sort_keys=True).lower()

    assert result.observed_at == "2025-01-01T12:05:00Z"
    assert "observed" in encoded
    assert "likely" in encoded
    assert "guaranteed" not in encoded
    assert "certain" not in encoded


def test_stale_and_exempt_controls_are_boolean_and_observation_time_is_required():
    with pytest.raises(DuelFirstValidationError, match="must be boolean"):
        observe(0, 1, api_stale="yes")
    with pytest.raises(DuelFirstValidationError, match="observed_at"):
        observe(0, 1, observed_at=None)
