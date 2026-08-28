from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
import requests

from api.clash_client import (
    AuthenticationError,
    BadRequestError,
    ClashRoyaleClient,
    EmptyResponseError,
    ForbiddenError,
    NotFoundError,
    RateLimitError,
    UpstreamServerError,
    encode_tag,
    normalize_tag,
)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, *, content=None, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        if content is None:
            content = b"{}" if payload is not None else b""
        self.content = content

    def json(self):
        return self._payload


def make_client(requester, **options):
    options.setdefault("api_key", "test-only-key")
    options.setdefault("requester", requester)
    options.setdefault("sleep_fn", Mock())
    options.setdefault(
        "clock",
        lambda: datetime(2026, 8, 27, 8, 0, tzinfo=timezone.utc),
    )
    return ClashRoyaleClient(**options)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("TAG", "TAG"), ("#TAG", "TAG"), ("%23TAG", "TAG")],
)
def test_normalize_tag_accepts_plain_hash_and_encoded_hash(raw, expected):
    assert normalize_tag(raw) == expected
    assert encode_tag(raw) == "%23TAG"


def test_request_path_is_encoded_and_auth_timeout_are_server_side():
    requester = Mock(return_value=FakeResponse(payload={"tag": "#TAG"}))
    client = make_client(requester, timeout=7)

    result = client.get_player("%23tag")

    requester.assert_called_once_with(
        "https://proxy.royaleapi.dev/v1/players/%23TAG",
        headers={"Authorization": "Bearer test-only-key"},
        timeout=7.0,
    )
    assert result.data == {"tag": "#TAG"}
    assert "test-only-key" not in repr(client)
    assert "test-only-key" not in repr(result)


def test_successful_json_response_has_freshness_and_safe_rate_limit_metadata():
    requester = Mock(
        return_value=FakeResponse(
            payload={"name": "Brabant Royale"},
            headers={
                "X-RateLimit-Limit": "100",
                "X-RateLimit-Remaining": "97",
                "X-RateLimit-Reset": "1770000000",
                "Retry-After": "2",
                "Authorization": "Bearer should-not-be-exposed",
            },
        )
    )
    result = make_client(requester).get_clan("TAG")

    assert result.data == {"name": "Brabant Royale"}
    assert result.metadata.source == "royaleapi_proxy"
    assert result.metadata.fetched_at == "2026-08-27T08:00:00Z"
    assert result.metadata.is_stale is False
    assert result.metadata.data_status == "fresh"
    assert result.metadata.rate_limit.remaining == 97
    assert result.metadata.rate_limit["retry_after"] == 2.0
    assert "Authorization" not in result.metadata.as_dict()
    assert "should-not-be-exposed" not in repr(result.metadata)


def test_empty_upstream_body_is_explicit_error_and_not_zero_data():
    requester = Mock(return_value=FakeResponse(status_code=200, content=b""))

    with pytest.raises(EmptyResponseError) as raised:
        make_client(requester, stale_if_error=False).get_members("TAG")

    assert raised.value.code == "empty_response"
    assert raised.value.status_code == 200
    assert "0" not in str(raised.value)


def test_empty_json_object_is_marked_empty_instead_of_becoming_zero_data():
    requester = Mock(return_value=FakeResponse(payload={}))

    result = make_client(requester).get_current_river_race("TAG")

    assert result.data == {}
    assert result.metadata.empty is True
    assert result.metadata.data_status == "empty"
    assert result.metadata.is_stale is False


@pytest.mark.parametrize(
    ("status", "expected_error"),
    [
        (400, BadRequestError),
        (401, AuthenticationError),
        (403, ForbiddenError),
        (404, NotFoundError),
        (429, RateLimitError),
        (500, UpstreamServerError),
        (503, UpstreamServerError),
    ],
)
def test_http_statuses_map_to_stable_error_types(status, expected_error):
    secret = "test-only-key"
    requester = Mock(
        return_value=FakeResponse(
            status_code=status,
            payload={"message": secret},
            headers={"Retry-After": "3"} if status == 429 else {},
        )
    )

    with pytest.raises(expected_error) as raised:
        make_client(
            requester,
            api_key=secret,
            max_retries=0,
            stale_if_error=False,
        ).get_player("TAG")

    assert raised.value.status_code == status
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)
    if status == 429:
        assert raised.value.rate_limit.retry_after_seconds == 3.0


def test_timeout_and_temporary_5xx_are_retried_with_bounded_exponential_backoff():
    requester = Mock(
        side_effect=[
            requests.Timeout(),
            FakeResponse(status_code=503, payload={"error": "temporary"}),
            FakeResponse(payload={"ok": True}),
        ]
    )
    sleep = Mock()
    client = make_client(
        requester,
        max_retries=2,
        backoff_factor=0.25,
        max_backoff=1,
        sleep_fn=sleep,
    )

    result = client.get_player_battlelog("#TAG")

    assert result.data == {"ok": True}
    assert requester.call_count == 3
    assert [call.args[0] for call in sleep.call_args_list] == [0.25, 0.5]
    assert result.metadata.attempts == 3


def test_stale_snapshot_is_returned_with_old_fetched_at_after_failed_refresh():
    requester = Mock(
        side_effect=[
            FakeResponse(payload={"tag": "#TAG", "members": 10}),
            FakeResponse(status_code=500, payload={"secret": "not-returned"}),
        ]
    )
    client = make_client(requester, max_retries=0)

    fresh = client.get_clan("TAG")
    stale = client.get_clan("%23TAG")

    assert fresh.metadata.is_stale is False
    assert stale.data == {"tag": "#TAG", "members": 10}
    assert stale.metadata.is_stale is True
    assert stale.metadata.stale is True
    assert stale.metadata.data_status == "stale"
    assert stale.metadata.fetched_at == fresh.metadata.fetched_at
    assert stale.metadata.error_code == "upstream_server_error"
    assert "not-returned" not in repr(stale)


@pytest.mark.parametrize(
    ("method_name", "tag", "expected_path"),
    [
        ("get_clan", "TAG", "/clans/%23TAG"),
        ("get_members", "#TAG", "/clans/%23TAG/members"),
        (
            "get_current_river_race",
            "%23TAG",
            "/clans/%23TAG/currentriverrace",
        ),
        ("get_river_race_log", "TAG", "/clans/%23TAG/riverracelog"),
        ("get_player", "TAG", "/players/%23TAG"),
        ("get_player_battlelog", "TAG", "/players/%23TAG/battlelog"),
    ],
)
def test_all_supported_methods_use_official_paths(method_name, tag, expected_path):
    requester = Mock(return_value=FakeResponse(payload={"ok": True}))
    result = getattr(make_client(requester), method_name)(tag)

    assert result.data == {"ok": True}
    assert requester.call_args.args[0] == (
        "https://proxy.royaleapi.dev/v1" + expected_path
    )


def test_no_network_is_needed_when_http_requester_is_injected():
    requester = Mock(return_value=FakeResponse(payload={"ok": True}))

    client = ClashRoyaleClient(
        api_key="test-only-key",
        requester=requester,
        sleep_fn=lambda _delay: None,
    )
    client.get_river_race_log("TAG", limit=5)

    assert requester.call_args.args[0].endswith("/clans/%23TAG/riverracelog?limit=5")
