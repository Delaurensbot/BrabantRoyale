import io
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import api.leader_decisions as leader_decision_route
import api.scouting as scouting_route
from api.clash_client import NotFoundError
from clan_policy import ADMIN_KEY_HEADER
from scouting_metrics import (
    DESTINATIONS,
    T15_DESTINATION_TO_DECISION_TYPE,
    build_fit_payload,
    build_profile_metrics,
    build_war_metrics,
)


CLAN_TAG = "9YP8UY"


def profile(
    *,
    clan_tag=CLAN_TAG,
    trophies=9000,
    best_trophies=9000,
    card_level=16,
    card_count=40,
):
    return {
        "tag": "#PLAYER1",
        "name": "Alice",
        "clan": {"tag": f"#{clan_tag}", "name": "Brabant Royale"},
        "trophies": trophies,
        "bestTrophies": best_trophies,
        "cards": [
            {"level": card_level, "maxLevel": 16}
            for _ in range(card_count)
        ],
    }


def war_rows(*, count=6, decks=16, contribution=3000):
    return [
        {
            "race_created_at": f"2026-01-{index + 1:02d}T09:00:00Z",
            "clan_tag": CLAN_TAG,
            "player_tag": "PLAYER1",
            "contribution": contribution,
            "decks_used": decks,
        }
        for index in range(count)
    ]


def test_t15_payload_exposes_separate_contract_sections_and_exact_destinations():
    payload = build_fit_payload(profile(), war_rows(), clan_tag=CLAN_TAG)

    assert set(DESTINATIONS) == {"main", "BR2", "BR3", "trial", "reject"}
    assert payload["account_readiness"]["score"] is not None
    assert payload["observed_war_reliability"]["sample_size"] == 6
    assert payload["trial_status"]["status"] == "complete"
    assert payload["recommendation"]["destination"] == "main"
    assert payload["recommendation"]["recommended_destination"] == "main"
    assert payload["recommendation"]["sample_size"] == 6
    assert payload["recommendation"]["reason"]
    assert payload["manual_intake_checklist"]
    assert payload["screening_policy"]["destinations"] == list(DESTINATIONS)


def test_external_candidate_has_no_war_score_and_is_trial():
    payload = build_fit_payload(profile(clan_tag="OTHER"), [], clan_tag=CLAN_TAG)

    assert payload["mode"] == "extern"
    assert payload["profile"]["score"] is not None
    assert payload["war"]["sample_size"] == 0
    assert payload["war"]["score"] is None
    assert payload["war"]["reliability"] is None
    assert payload["war"]["missed_attacks"] is None
    assert payload["war"]["average_contribution"] is None
    assert payload["recommendation"]["destination"] == "trial"
    assert payload["recommendation"]["score_basis"] == "account_readiness_only"
    assert payload["recommendation"]["war_data_sufficient"] is False
    assert payload["war"]["observation_scope"] == "own_clan_history"
    assert payload["war"]["is_own_observation"] is True
    assert any("unknown" in reason.lower() for reason in payload["war"]["reasons"])


def test_one_observed_race_remains_trial_without_a_full_war_score():
    payload = build_fit_payload(profile(), war_rows(count=1), clan_tag=CLAN_TAG)

    assert payload["war"]["sample_size"] == 1
    assert payload["war"]["reliability"] == 100.0
    assert payload["war"]["score"] is None
    assert payload["trial_status"]["status"] == "in_progress"
    assert payload["recommendation"]["destination"] == "trial"


def test_partial_war_metrics_are_unknown_and_cannot_unlock_destination():
    rows = war_rows(count=2)
    rows[1]["decks_used"] = None
    payload = build_fit_payload(profile(), rows, clan_tag=CLAN_TAG)

    assert payload["war"]["sample_size"] == 2
    assert payload["war"]["reliability"] is None
    assert payload["war"]["missed_attacks"] is None
    assert payload["war"]["score"] is None
    assert payload["war"]["status"] == "unknown"
    assert payload["recommendation"]["destination"] == "trial"
    assert payload["trial_status"]["is_required"] is True
    assert "onzekerheid" in payload["war"]["reason"].lower()


def test_missing_profile_fields_stay_unknown_instead_of_becoming_zero():
    payload = build_fit_payload(
        {"tag": "#PLAYER1", "name": "Alice"},
        [],
        clan_tag=CLAN_TAG,
    )
    profile_metrics = payload["profile"]

    assert profile_metrics["score"] is None
    assert profile_metrics["trophies"] is None
    assert profile_metrics["best_trophies"] is None
    assert profile_metrics["card_level"] is None
    assert profile_metrics["cards_level_15_plus"] is None
    assert profile_metrics["cards_level_16"] is None
    assert profile_metrics["deck_breadth"] is None
    assert profile_metrics["sample_size"] == 0
    assert payload["recommendation"]["destination"] == "trial"
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "None" not in encoded


def test_sufficient_war_data_with_unknown_account_readiness_stays_trial():
    payload = build_fit_payload(
        {
            "tag": "#PLAYER1",
            "name": "Alice",
            "clan": {"tag": f"#{CLAN_TAG}", "name": "Brabant Royale"},
        },
        war_rows(),
        clan_tag=CLAN_TAG,
    )

    recommendation = payload["recommendation"]
    assert payload["war"]["sufficient"] is True
    assert payload["account_readiness"]["score"] is None
    assert recommendation["destination"] == "trial"
    assert recommendation["score"] is None
    assert "account readiness: unknown" in recommendation["reason"].lower()


@pytest.mark.parametrize(
    ("payload_profile", "rows", "expected"),
    [
        (profile(), war_rows(), "main"),
        (profile(), war_rows(decks=15, contribution=2500), "BR2"),
        (profile(card_level=14, card_count=8, trophies=5000, best_trophies=6000), war_rows(decks=14, contribution=2000), "BR3"),
        (profile(), war_rows(decks=10, contribution=3000), "reject"),
        (profile(clan_tag="OTHER"), war_rows(count=1), "trial"),
    ],
)
def test_destination_policy_covers_all_contract_values(payload_profile, rows, expected):
    payload = build_fit_payload(payload_profile, rows, clan_tag=CLAN_TAG)

    assert payload["recommendation"]["destination"] == expected
    assert payload["recommendation"]["destination"] in DESTINATIONS
    assert payload["recommendation"]["sample_size"] == len(rows)
    assert payload["recommendation"]["reasons"]


def test_war_rows_are_scoped_to_candidate_and_missing_values_are_not_fabricated():
    metrics = build_war_metrics(
        [
            war_rows(count=1)[0],
            {
                **war_rows(count=1)[0],
                "race_created_at": "2026-01-02T09:00:00Z",
                "player_tag": "OTHERPLAYER",
            },
            {
                **war_rows(count=1)[0],
                "race_created_at": "2026-01-03T09:00:00Z",
                "contribution": 0,
                "decks_used": 0,
            },
        ]
    )

    # The pure metric function documents its input as already scoped; it still
    # keeps the row identity visible and does not turn the zero row into a
    # played zero-reliability race.
    assert metrics["observed_races"] == 3
    assert metrics["sample_size"] == 2
    assert metrics["zero_contribution_races"] == 1
    assert metrics["reliability"] == 100.0
    assert metrics["missed_attacks"] == 0


def test_empty_and_invalid_war_rows_are_explicit():
    empty = build_war_metrics([])
    invalid = build_war_metrics([None, {}, {"race_created_at": ""}])

    for result in (empty, invalid):
        assert result["score"] is None
        assert result["reliability"] is None
        assert result["missed_attacks"] is None
        assert result["average_contribution"] is None
    assert invalid["invalid_rows"] == 3


def test_profile_card_depth_and_deck_breadth_are_exposed():
    result = build_profile_metrics(
        {
            "trophies": 7000,
            "bestTrophies": 8000,
            "cards": [
                {"level": 16, "maxLevel": 16},
                {"level": 15, "maxLevel": 16},
                {"level": 14, "maxLevel": 16},
                {"level": 13, "maxLevel": 16},
            ],
            "clan": {"tag": "#GPCLVLPP", "name": "Brabant Royale 2"},
        }
    )

    assert result["card_level"] == 16
    assert result["cards_level_15_plus"] == 2
    assert result["cards_level_16"] == 1
    assert result["deck_breadth"] == 3
    assert result["viable_decks"] == 0
    assert result["current_clan_tag"] == "GPCLVLPP"


def test_card_level_without_rarity_maximum_is_partial_not_fabricated():
    result = build_profile_metrics({"cards": [{"level": 16}]})

    assert result["card_level"] == 16
    assert result["cards_level_16"] == 1
    assert result["field_status"]["card_level"] == "partial"
    assert result["data_status"] == "partial"


def test_scouting_route_rechecks_own_clan_and_player_identity():
    rows = [
        {
            "race_created_at": "2026-01-01T09:00:00Z",
            "clan_tag": CLAN_TAG,
            "player_tag": "PLAYER1",
            "contribution": 3000,
            "decks_used": 16,
        },
        {
            "race_created_at": "2026-01-02T09:00:00Z",
            "clan_tag": "OTHERCLAN",
            "player_tag": "PLAYER1",
            "contribution": 3000,
            "decks_used": 16,
        },
        {
            "race_created_at": "2026-01-02T10:00:00Z",
            "player_tag": "PLAYER1",
            "contribution": 3000,
            "decks_used": 16,
        },
        {
            "race_created_at": "2026-01-03T09:00:00Z",
            "clan_tag": CLAN_TAG,
            "player_tag": "OTHERPLAYER",
            "contribution": 3000,
            "decks_used": 16,
        },
    ]

    assert [
        row["race_created_at"]
        for row in scouting_route._filter_own_history_rows(
            rows,
            clan_tag=CLAN_TAG,
            player_tag="PLAYER1",
        )
    ] == ["2026-01-01T09:00:00Z"]


def test_scouting_route_rejects_history_without_clan_identity():
    row = {
        "race_created_at": "2026-01-01T09:00:00Z",
        "player_tag": "PLAYER1",
        "contribution": 3000,
        "decks_used": 16,
    }

    assert scouting_route._own_history_row_key(
        row,
        clan_tag=CLAN_TAG,
        player_tag="PLAYER1",
    ) is None
    assert scouting_route._filter_own_history_rows(
        [row],
        clan_tag=CLAN_TAG,
        player_tag="PLAYER1",
    ) == []


def test_parse_query_rejects_duplicate_or_unknown_clan_without_fallback():
    with pytest.raises(ValueError):
        scouting_route.parse_query("/api/scouting?tag=%23PLAYER1&tag=OTHER")
    with pytest.raises(ValueError):
        scouting_route.parse_query("/api/scouting?tag=%23PLAYER1&clan=UNKNOWNCLAN")
    with pytest.raises(ValueError):
        scouting_route.parse_query("/api/scouting?tag=not%2Ftag")


def test_fetch_player_uses_t01_client_and_t02_normalizer_without_raw_requests():
    response = Mock()
    normalized = Mock()
    with patch.object(scouting_route, "ClashRoyaleClient") as client_class, patch.object(
        scouting_route, "normalize_player_profile", return_value=normalized
    ) as normalize, patch.object(
        scouting_route,
        "serialize_normalized",
        return_value={"player_tag": "PLAYER1", "name": "Alice"},
    ) as serialize:
        client_class.return_value.get_player.return_value = response

        result = scouting_route.fetch_player("PLAYER1", "test-key")

    client_class.assert_called_once_with(api_key="test-key")
    client_class.return_value.get_player.assert_called_once_with("PLAYER1")
    normalize.assert_called_once_with(response, player_tag="PLAYER1")
    serialize.assert_called_once_with(normalized)
    assert result["player_tag"] == "PLAYER1"


def test_clash_not_found_error_is_mapped_without_echoing_internal_details():
    status, message = scouting_route.classify_error(
        NotFoundError("private upstream detail", endpoint="/players/PLAYER1")
    )

    assert status == 404
    assert message == "Speler niet gevonden. Controleer de player tag."
    assert "private" not in message


def test_t15_destination_mapping_reuses_existing_t13_decision_types():
    assert T15_DESTINATION_TO_DECISION_TYPE == {
        "main": "main_clan",
        "BR2": "BR2",
        "BR3": "BR3",
        "trial": "strategic_experiment",
        "reject": "reject",
    }


def _fake_decision_request(*, body, headers):
    request = type("Request", (), {})()
    request.path = "/api/leader_decisions"
    request.headers = dict(headers)
    encoded = json.dumps(body).encode("utf-8")
    request.rfile = io.BytesIO(encoded)
    request.headers["Content-Length"] = str(len(encoded))
    request._send_json = lambda status, payload: setattr(
        request,
        "sent",
        (status, payload),
    )
    return request


def test_t15_manual_decision_payload_uses_existing_admin_only_route():
    body = {
        "clan_tag": CLAN_TAG,
        "player_tag": "PLAYER1",
        "actor": "leader-1",
        "decision_type": T15_DESTINATION_TO_DECISION_TYPE["trial"],
        "reason": "T15 destination=trial; onvoldoende eigen war-data.",
        "related_race_key": "T15-scouting-PLAYER1-trial",
        "idempotency_key": "t15-PLAYER1-trial-1",
    }
    request = _fake_decision_request(
        body=body,
        headers={ADMIN_KEY_HEADER: "test-admin-key"},
    )
    stored = {"ok": True, "status": "stored", "decision": body}
    with patch.object(
        leader_decision_route,
        "write_leader_decision",
        return_value=stored,
    ) as write_decision:
        leader_decision_route.handler.do_POST(request)

    assert request.sent[0] == 200
    assert write_decision.call_args.args == (body, "test-admin-key")
    assert "test-admin-key" not in json.dumps(request.sent[1])


def test_t15_manual_decision_ui_contract_posts_admin_header_without_embedded_secret():
    html_path = Path(__file__).resolve().parents[1] / "analytics.html"
    html = html_path.read_text(encoding="utf-8")

    assert "fetch('/api/leader_decisions'" in html
    assert "method: 'POST'" in html
    assert "'X-Analytics-Admin-Key': adminKey" in html
    assert "decision_type: SCOUTING_DECISION_TYPES[destination]" in html
    assert "T15 destination=" in html
    assert "CLASH_ROYALE_API_KEY" not in html
    assert "SUPABASE_SERVICE_ROLE_KEY" not in html
    assert "SUPABASE_SECRET_KEY" not in html
