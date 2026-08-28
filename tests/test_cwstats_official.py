import json

import pytest

from api import cwstats
from api.clash_client import (
    ClashResponse,
    ForbiddenError,
    ResponseMetadata,
)
from api.clash_normalizers import (
    PlayerProfile,
    RaceClan,
    RaceContext,
    RaceParticipant,
)


CLAN_TAG = "9YP8UY"
FETCHED_AT = "2026-08-27T08:00:00Z"


def envelope(payload, endpoint, *, stale=False, empty=False):
    return ClashResponse(
        data=payload,
        metadata=ResponseMetadata(
            source="test_official",
            fetched_at=FETCHED_AT,
            is_stale=stale,
            stale_reason="upstream_server_error" if stale else None,
            error_code="upstream_server_error" if stale else None,
            endpoint=endpoint,
            empty=empty,
        ),
    )


def participant(tag="#PLAYER1", name="Alice", *, fame=2500, decks_today=4):
    return {
        "tag": tag,
        "name": name,
        "fame": fame,
        "repairPoints": 100,
        "decksUsed": decks_today,
        "decksUsedToday": decks_today,
        "boatAttacks": 1,
        "boatAttacksToday": 1,
        "boatDefenses": 2,
        "boatDefensesToday": 1,
    }


def clan_payload():
    return {
        "tag": f"#{CLAN_TAG}",
        "name": "Brabant Royale",
        "type": "open",
        "members": 2,
        "clanScore": 4320,
        "clanWarTrophies": 2810,
        "badgeId": 123,
    }


def members_payload():
    return {
        "items": [
            {
                "tag": "#PLAYER1",
                "name": "Alice",
                "role": "leader",
                "trophies": 6500,
            },
            {
                "tag": "%23PLAYER2",
                "name": "Bob",
                "role": "member",
                "trophies": 6200,
            },
        ]
    }


def race_payload():
    own = {
        "tag": f"#{CLAN_TAG}",
        "name": "Brabant Royale",
        "clanScore": 4320,
        "fame": 2500,
        "repairPoints": 100,
        "decksUsedToday": 4,
        "participants": [participant()],
    }
    opponent = {
        "tag": "#OPPONENT",
        "name": "Opponent Clan",
        "clanScore": 4100,
        "fame": 2100,
        "repairPoints": 90,
        "decksUsedToday": 2,
        "participants": [],
    }
    return {
        "state": "warDay",
        "seasonId": 134,
        "sectionIndex": 3,
        "periodIndex": 2,
        "periodType": "war",
        "createdDate": "20260727T094301.000Z",
        "clan": own,
        "clans": [own, opponent],
    }


class FakeClient:
    def __init__(self, *, clan=None, members=None, race=None):
        self.responses = {
            "clan": clan
            if clan is not None
            else envelope(clan_payload(), f"/clans/%23{CLAN_TAG}"),
            "members": members
            if members is not None
            else envelope(
                members_payload(), f"/clans/%23{CLAN_TAG}/members"
            ),
            "race": race
            if race is not None
            else envelope(
                race_payload(), f"/clans/%23{CLAN_TAG}/currentriverrace"
            ),
        }
        self.calls = []

    def get_clan(self, tag):
        self.calls.append(("clan", tag))
        response = self.responses["clan"]
        if isinstance(response, Exception):
            raise response
        return response

    def get_members(self, tag):
        self.calls.append(("members", tag))
        response = self.responses["members"]
        if isinstance(response, Exception):
            raise response
        return response

    def get_current_river_race(self, tag):
        self.calls.append(("race", tag))
        response = self.responses["race"]
        if isinstance(response, Exception):
            raise response
        return response


def test_official_client_calls_all_routes_with_normalized_tag_and_models():
    client = FakeClient()

    snapshot = cwstats.fetch_official_snapshot("%239yp8uy", client=client)

    assert client.calls == [
        ("clan", CLAN_TAG),
        ("members", CLAN_TAG),
        ("race", CLAN_TAG),
    ]
    assert isinstance(snapshot.clan, RaceClan)
    assert isinstance(snapshot.members[0], PlayerProfile)
    assert isinstance(snapshot.race.context, RaceContext)
    assert isinstance(snapshot.race.clans[0], RaceClan)
    assert isinstance(snapshot.race.participants[0], RaceParticipant)


def test_successful_official_payload_keeps_legacy_fields_and_metadata():
    client = FakeClient()
    payload = cwstats.build_cwstats_payload(
        "/api/cwstats?clan=%239yp8uy",
        client=client,
    )

    assert payload["ok"] is True
    assert payload["clan_tag"] == CLAN_TAG
    assert payload["clan_name"] == "Brabant Royale"
    assert payload["source"] == "royaleapi_proxy"
    assert payload["fetched_at"] == FETCHED_AT
    assert payload["data_status"] == "fresh"
    assert payload["data_quality"]["normalizers"] == [
        "RaceContext",
        "RaceClan",
        "RaceParticipant",
        "PlayerProfile",
    ]
    assert payload["race_rows"]
    assert payload["race_rows"][0]["currentMedals"] == 2500
    assert payload["race_rows"][0]["source"] == "royaleapi_proxy"
    assert "Brabant Royale" in payload["race_overview_text"]
    assert "Alice" in payload["players_text"]
    assert payload["clan_access_type"] == "Open"
    assert [record["endpoint"] for record in payload["endpoint_metadata"]] == [
        f"/clans/%23{CLAN_TAG}",
        f"/clans/%23{CLAN_TAG}/members",
        f"/clans/%23{CLAN_TAG}/currentriverrace",
    ]


def test_empty_current_race_is_explicit_and_does_not_create_strategy_or_zeros():
    client = FakeClient(
        race=envelope(
            {},
            f"/clans/%23{CLAN_TAG}/currentriverrace",
            empty=True,
        )
    )

    payload = cwstats.build_cwstats_payload(
        "/api/cwstats?clan=%23%39YP8UY",
        client=client,
    )

    assert payload["ok"] is True
    assert payload["data_status"] == "empty"
    assert payload["metadata"]["data_status"] == "empty"
    assert payload["race_rows"] == []
    assert payload["total_players_participated"] is None
    assert payload["finish_outlook"]["data_status"] == "empty"
    assert "geen actieve officiële current river race" in payload["race_overview_text"].lower()
    assert all(
        row[metric] is None
        for row in payload["race_rows"]
        for metric in ("currentMedals", "decksUsedToday")
    )


def test_one_official_endpoint_error_is_safe_and_never_exposes_body_or_zero_metrics():
    client = FakeClient(
        race=ForbiddenError(
            "upstream secret body must not escape",
            endpoint=f"/clans/%23{CLAN_TAG}/currentriverrace",
            status_code=403,
        )
    )

    payload = cwstats.build_cwstats_payload(
        "/api/cwstats?clan=%239YP8UY",
        client=client,
    )

    assert payload["ok"] is False
    assert payload["http_ok"] is True
    assert payload["data_status"] == "partial"
    assert payload["data_quality"]["errors"] == [
        {
            "endpoint": f"/clans/%23{CLAN_TAG}/currentriverrace",
            "code": "forbidden",
            "status_code": 403,
        }
    ]
    assert payload["race_rows"]
    assert payload["race_rows"][0]["medals"] is None
    assert payload["race_rows"][0]["decks"]["used"] is None
    assert "upstream secret body" not in json.dumps(payload)
    assert "Authorization" not in json.dumps(payload)
    assert "0/0" not in payload["players_text"]
    assert "| 0 |" not in payload["players_text"]


def test_stale_metadata_is_forwarded_without_secret_fields():
    client = FakeClient(
        race=envelope(
            race_payload(),
            f"/clans/%23{CLAN_TAG}/currentriverrace",
            stale=True,
        )
    )

    payload = cwstats.build_cwstats_payload(
        "/api/cwstats?clan=%239YP8UY",
        client=client,
    )

    assert payload["data_status"] == "stale"
    assert payload["metadata"]["is_stale"] is True
    assert payload["metadata"]["stale_reason"] == "upstream_server_error"
    race_record = payload["endpoint_metadata"][2]
    assert race_record["is_stale"] is True
    assert race_record["data_status"] == "stale"
    assert "Authorization" not in json.dumps(payload)


def test_legacy_fallback_keyword_is_ignored_and_official_errors_stay_explicit():
    client = FakeClient(
        members=ForbiddenError(
            "private upstream body",
            endpoint=f"/clans/%23{CLAN_TAG}/members",
            status_code=403,
        )
    )

    payload = cwstats.build_cwstats_payload(
        "/api/cwstats?clan=%239YP8UY",
        client=client,
        allow_html_fallback=True,
    )

    assert payload["source"] == "royaleapi_proxy"
    assert payload["data_quality"]["fallback"] == {
        "used": False,
        "temporary": False,
        "official_data_precedence": True,
        "source": None,
    }
    assert payload["ok"] is False
    assert payload["data_status"] == "partial"
    assert "private upstream body" not in json.dumps(payload)


@pytest.mark.parametrize("query", ["", "#9yp8uy", "%239yp8uy", "%25239yp8uy"])
def test_existing_query_forms_resolve_to_same_official_endpoint_paths(query):
    client = FakeClient()

    path = "/api/cwstats"
    if query:
        path += f"?clan={query}"
    payload = cwstats.build_cwstats_payload(path, client=client)

    assert payload["clan_tag"] == CLAN_TAG
    assert payload["endpoint_metadata"][2]["endpoint"] == (
        f"/clans/%23{CLAN_TAG}/currentriverrace"
    )
