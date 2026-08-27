import json
from unittest.mock import patch

from api.clash_client import ClashResponse, ResponseMetadata
from api.clash_normalizers import (
    DATA_STATUS_EMPTY,
    DATA_STATUS_PARTIAL,
    DATA_STATUS_STALE,
    PlayerProfile,
    RaceClan,
    RaceContext,
    RaceParticipant,
    normalize_clan,
    normalize_current_river_race,
    normalize_members,
    normalize_player_profile,
    normalize_race_participants,
    serialize_normalized,
)


CLAN_TAG = "9YP8UY"


def envelope(payload, *, stale=False):
    return ClashResponse(
        data=payload,
        metadata=ResponseMetadata(
            source="test_fixture",
            fetched_at="2026-08-27T08:00:00Z",
            is_stale=stale,
            stale_reason="upstream_server_error" if stale else None,
            error_code="upstream_server_error" if stale else None,
        ),
    )


def participant(tag="#PLAYER1", name="Alice"):
    return {
        "tag": tag,
        "name": name,
        "fame": 2500,
        "repairPoints": 100,
        "decksUsed": 16,
        "decksUsedToday": 4,
        "boatAttacks": 1,
        "boatAttacksToday": 1,
        "boatDefenses": 2,
        "boatDefensesToday": 1,
    }


def current_race_payload():
    return {
        "state": "warDay",
        "seasonId": 134,
        "sectionIndex": 3,
        "periodIndex": 2,
        "periodType": "war",
        "createdDate": "20260727T094301.000Z",
        "clan": {
            "tag": "#9YP8UY",
            "name": "Brabant Royale",
            "clanScore": 4320,
            "fame": 2500,
            "repairPoints": 100,
            "decksUsedToday": 4,
            "participants": [participant()],
        },
        "clans": [
            {
                "tag": "#9YP8UY",
                "name": "Brabant Royale",
                "clanScore": 4320,
                "participants": [participant()],
            },
            {
                "tag": "%23OPPONENT",
                "name": "Opponent Clan",
                "clanScore": 4100,
                "fame": 2100,
                "participants": [],
            },
        ],
    }


def test_normalizes_current_river_race_envelope_and_all_minimum_fields():
    normalized = normalize_current_river_race(
        envelope(current_race_payload()),
        clan_tag="%239yp8uy",
    )

    assert isinstance(normalized.context, RaceContext)
    assert normalized.context.clan_tag == CLAN_TAG
    assert normalized.context.season_id == 134
    assert normalized.context.section_index == 3
    assert normalized.context.period_index == 2
    assert normalized.context.period_type == "war"
    assert normalized.context.state == "warDay"
    assert normalized.context.race_created_at == "2026-07-27T09:43:01Z"
    assert normalized.context.captured_at == "2026-08-27T08:00:00Z"
    assert normalized.context.source == "test_fixture"

    assert isinstance(normalized.clans[0], RaceClan)
    assert normalized.clans[0].is_opponent is False
    assert normalized.clans[1].is_opponent is True

    row = normalized.participants[0]
    assert isinstance(row, RaceParticipant)
    assert row.player_tag == "PLAYER1"
    assert row.name == "Alice"
    assert row.role is None
    assert row.fame == 2500
    assert row.repair_points == 100
    assert row.decks_used == 16
    assert row.decks_used_today == 4
    assert row.boat_attacks == 1
    assert row.boat_attacks_today == 1
    assert row.boat_defenses == 2
    assert row.boat_defenses_today == 1


def test_normalizes_race_log_items_and_standings_clan_variant():
    payload = {
        "items": [
            {
                "seasonId": 128,
                "createdDate": "20250101T120000.000Z",
                "standings": [
                    {
                        "rank": 4,
                        "clan": {
                            "tag": "#9YP8UY",
                            "name": "Brabant Royale",
                            "fame": 7350,
                            "participants": [participant()],
                        },
                    }
                ],
            }
        ]
    }

    normalized = normalize_current_river_race(payload, clan_tag=CLAN_TAG)

    assert normalized.context.season_id == 128
    assert normalized.context.race_created_at == "2025-01-01T12:00:00Z"
    assert normalized.clans[0].clan_tag == CLAN_TAG
    assert normalized.clans[0].rank == 4
    assert normalized.participants[0].player_tag == "PLAYER1"


def test_normalizes_direct_clan_and_members_items_without_zero_defaults():
    clan = normalize_clan(
        {
            "tag": "#9yp8uy",
            "name": "Brabant Royale",
            "clanScore": 4320,
            "clanWarTrophies": 2810,
            "members": 41,
            "private_marker": "fixture-only-redacted-value",
        }
    )
    members = normalize_members(
        {
            "items": [
                {
                    "tag": "%23PLAYER1",
                    "name": "Alice",
                    "role": "elder",
                    "trophies": 6500,
                },
                {
                    "tag": "#PLAYER2",
                    "name": "Bob",
                    "role": "member",
                    "bestTrophies": 7000,
                },
            ]
        },
        clan_tag=CLAN_TAG,
    )

    assert clan.clan_tag == CLAN_TAG
    assert clan.member_count == 41
    assert clan.clan_type is None
    assert clan.clan_score == 4320
    assert clan.clan_war_trophies == 2810
    assert clan.fame is None
    assert clan.field_status["fame"] == "missing"
    assert [(row.player_tag, row.role) for row in members] == [
        ("PLAYER1", "elder"),
        ("PLAYER2", "member"),
    ]
    assert members[0].clan_tag == CLAN_TAG
    assert members[0].trophies == 6500
    assert members[0].best_trophies is None
    assert members[1].best_trophies == 7000


def test_duplicate_current_race_clan_views_merge_without_losing_totals():
    normalized = normalize_current_river_race(
        current_race_payload(),
        clan_tag=CLAN_TAG,
    )

    own = normalized.clans[0]
    assert own.clan_score == 4320
    assert own.fame == 2500
    assert own.repair_points == 100
    assert own.decks_used_today == 4


def test_normalizes_player_profile_cards_and_profile_fields():
    profile = normalize_player_profile(
        envelope(
            {
                "tag": "#PLAYER1",
                "name": "Alice",
                "trophies": 6500,
                "bestTrophies": 7000,
                "expLevel": 15,
                "wins": 123,
                "clan": {
                    "tag": "%239YP8UY",
                    "name": "Brabant Royale",
                    "clanScore": 4320,
                },
                "arena": {"id": 54000012, "name": "Legendary Arena"},
                "cards": [
                    {
                        "id": 26000000,
                        "name": "Knight",
                        "level": 14,
                        "maxLevel": 14,
                        "count": 500,
                        "starLevel": 1,
                    }
                ],
                "badges": [{"name": "War Hero", "level": 3}],
                "secret": "fixture-only-redacted-value",
            }
        )
    )

    assert isinstance(profile, PlayerProfile)
    assert profile.player_tag == "PLAYER1"
    assert profile.name == "Alice"
    assert profile.trophies == 6500
    assert profile.best_trophies == 7000
    assert profile.clan_tag == CLAN_TAG
    assert profile.clan_name == "Brabant Royale"
    assert profile.arena_name == "Legendary Arena"
    assert profile.cards[0].name == "Knight"
    assert profile.cards[0].card_id == 26000000
    assert profile.cards[0].max_level == 14
    assert profile.badges[0]["name"] == "War Hero"


def test_card_icon_urls_preserve_safe_https_and_reject_unsafe_values():
    profile = normalize_player_profile(
        {
            "tag": "#PLAYER1",
            "name": "Alice",
            "cards": [
                {
                    "name": "Knight",
                    "iconUrls": {
                        "medium": "https://cdn.example/card.png",
                        "small": "javascript:alert(1)",
                        "evolutionMedium": "https://user:pass@cdn.example/card.png",
                        "unsafeScheme": "file:///private/card.png",
                        "signed": "https://cdn.example/card.png?token=fixture-query-value",
                    },
                }
            ],
        }
    )

    icon_urls = profile.cards[0].icon_urls
    assert icon_urls["medium"] == "https://cdn.example/card.png"
    assert "small" not in icon_urls
    assert "evolutionMedium" not in icon_urls
    assert "signed" not in icon_urls
    assert "fixture-query-value" not in json.dumps(serialize_normalized(profile))


def test_empty_race_empty_items_and_missing_participants_are_explicit():
    empty_items = normalize_current_river_race({"items": []}, clan_tag=CLAN_TAG)
    missing_participants = normalize_current_river_race(
        {"clan": {"tag": CLAN_TAG, "name": "Brabant Royale"}},
        clan_tag=CLAN_TAG,
    )
    empty_participants = normalize_current_river_race(
        {"clan": {"tag": CLAN_TAG, "name": "Brabant Royale", "participants": []}},
        clan_tag=CLAN_TAG,
    )

    assert empty_items.context.data_status == DATA_STATUS_EMPTY
    assert empty_items.clans == ()
    assert empty_items.participants == ()
    assert missing_participants.clans[0].participants is None
    assert missing_participants.clans[0].field_status["participants"] == "missing"
    assert empty_participants.clans[0].participants == ()
    assert empty_participants.clans[0].field_status["participants"] == "empty"


def test_partial_participant_keeps_none_and_tracks_missing_and_null_fields():
    rows = normalize_race_participants(
        {
            "clan": {
                "tag": CLAN_TAG,
                "name": "Brabant Royale",
                "participants": [
                    {
                        "tag": "#PLAYER1",
                        "name": "Alice",
                        "fame": 100,
                        "repairPoints": None,
                        # decksUsed and boat fields are deliberately absent.
                    }
                ],
            }
        },
        clan_tag=CLAN_TAG,
    )

    row = rows[0]
    assert row.fame == 100
    assert row.repair_points is None
    assert row.decks_used is None
    assert row.boat_attacks_today is None
    assert row.field_status["repair_points"] == "null"
    assert row.field_status["decks_used"] == "missing"
    assert row.data_status == DATA_STATUS_PARTIAL


def test_unexpected_types_nulls_and_unknown_fields_do_not_raise():
    normalized = normalize_current_river_race(
        {
            "seasonId": [],
            "sectionIndex": {"wrong": True},
            "periodIndex": None,
            "clan": {
                "tag": None,
                "name": 42,
                "participants": "not-an-array",
                "apiKey": "fixture-only-redacted-value",
            },
            "clans": None,
            "unexpected": object(),
        },
        clan_tag="#9YP8UY",
    )
    profile = normalize_player_profile(
        {"tag": None, "name": [], "cards": {"wrong": True}, "apiKey": "fixture-only-redacted-value"}
    )

    assert normalized.clans == ()
    assert normalized.context.section_index is None
    assert normalized.context.field_status["section_index"] == "invalid"
    assert profile.player_tag is None
    assert profile.cards is None
    assert profile.field_status["cards"] == "invalid"
    json.dumps(serialize_normalized(normalized))
    json.dumps(serialize_normalized(profile))


def test_tags_are_identity_and_name_changes_do_not_duplicate_players():
    members = normalize_members(
        {
            "items": [
                {"tag": "#player1", "name": "Old name"},
                {"tag": "%23PLAYER1", "name": "New name"},
                {"tag": "PLAYER2", "name": "Other"},
            ]
        }
    )

    assert len(members) == 2
    assert members[0].player_tag == "PLAYER1"
    assert members[0].name == "New name"
    assert members[1].player_tag == "PLAYER2"


def test_stale_envelope_metadata_is_preserved_for_downstream_use():
    normalized = normalize_current_river_race(
        envelope(current_race_payload(), stale=True),
        clan_tag=CLAN_TAG,
    )

    assert normalized.source == "test_fixture"
    assert normalized.captured_at == "2026-08-27T08:00:00Z"
    assert normalized.fetched_at == "2026-08-27T08:00:00Z"
    assert normalized.data_status == DATA_STATUS_STALE
    assert normalized.is_stale is True
    assert normalized.stale_reason == "upstream_server_error"
    assert normalized.error_code == "upstream_server_error"
    assert normalized.context.data_status == DATA_STATUS_STALE
    assert normalized.participants[0].data_status == DATA_STATUS_STALE


def test_serialization_is_safe_and_contains_no_arbitrary_upstream_fields():
    profile = normalize_player_profile(
        {
            "tag": "#PLAYER1",
            "name": "Alice",
            "trophies": None,
            "api_key": "fixture-only-redacted-value",
            "webhook": "fixture-only-redacted-value",
            "cards": [{"name": "Knight", "level": 14, "private": "fixture-only-redacted-value"}],
        }
    )

    serialized = serialize_normalized(profile)
    encoded = json.dumps(serialized, sort_keys=True)

    assert serialized["trophies"] is None
    assert "api_key" not in encoded
    assert "webhook" not in encoded
    assert "fixture-only-redacted-value" not in encoded
    assert serialized["cards"][0]["name"] == "Knight"


def test_normalizers_do_not_make_a_network_call():
    with patch("api.clash_client.requests.get") as mocked_get:
        normalize_current_river_race(current_race_payload(), clan_tag=CLAN_TAG)
        normalize_members({"items": [{"tag": "#PLAYER1", "name": "Alice"}]})
        normalize_player_profile({"tag": "#PLAYER1", "name": "Alice"})

    mocked_get.assert_not_called()
