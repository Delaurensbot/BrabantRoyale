"""Small, server-side client for the official Clash Royale API.

The repository uses ``https://proxy.royaleapi.dev/v1`` as the transport path
to the official Supercell API.  The proxy does not change the endpoint shape:
clan and player tags still need a URL-encoded ``#`` (``%23``).  This module
keeps that detail, authentication, retry policy, safe error mapping and
freshness metadata in one place.  It deliberately does not scrape HTML.

The client returns :class:`ClashResponse` envelopes instead of adding
metadata to an upstream payload.  That keeps upstream JSON intact while
making freshness explicit to callers.  A successful response is cached in
memory per client instance; when a later request fails, the last successful
payload can be returned with ``metadata.is_stale == True``.  Callers that
prefer exceptions for every failed refresh can set ``stale_if_error=False``.

Only a small allow-list of rate-limit headers is exposed.  Raw response
headers, authorization headers and upstream error bodies are never returned
or included in exception messages.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from builtins import TimeoutError as BuiltinTimeoutError
import math
import os
import re
import time
from typing import Any, Callable, Dict, Generic, Mapping, Optional, TypeVar
from urllib.parse import quote, unquote, urlsplit

import requests


ROYAL_API_BASE_URL = "https://proxy.royaleapi.dev/v1"
DEFAULT_BASE_URL = ROYAL_API_BASE_URL
DEFAULT_TIMEOUT = 25.0
MAX_TIMEOUT = 60.0
DEFAULT_MAX_RETRIES = 2
MAX_RETRIES = 5
DEFAULT_BACKOFF_FACTOR = 0.5
MAX_BACKOFF = 60.0
DEFAULT_SOURCE = "royaleapi_proxy"

_TAG_PATTERN = re.compile(r"^[A-Z0-9]+$")
_RETRYABLE_STATUSES = frozenset({408, 425, 429})

T = TypeVar("T")


class ClashClientError(Exception):
    """Base class for safe, stable client errors.

    The exception contains only a stable public message, endpoint path and
    status metadata.  It intentionally does not retain a requests exception,
    response body or response headers because those can contain sensitive
    information in some deployments.
    """

    code = "clash_client_error"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        endpoint: str = "",
        status_code: Optional[int] = None,
        attempts: int = 1,
        rate_limit: Optional["RateLimitMetadata"] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.endpoint = endpoint
        self.status_code = status_code
        self.attempts = attempts
        self.rate_limit = rate_limit

    @property
    def public_message(self) -> str:
        return self.message


class ConfigurationError(ClashClientError):
    code = "configuration_error"


class InvalidTagError(ClashClientError):
    code = "invalid_tag"


class InvalidRequestError(ClashClientError):
    code = "invalid_request"


class EmptyResponseError(ClashClientError):
    code = "empty_response"


class InvalidResponseError(ClashClientError):
    code = "invalid_response"


class ResponseDecodeError(InvalidResponseError):
    code = "invalid_json"


class TransportError(ClashClientError):
    code = "transport_error"
    retryable = True


class TimeoutError(TransportError):
    code = "timeout"


class BadRequestError(ClashClientError):
    code = "bad_request"


class AuthenticationError(ClashClientError):
    code = "authentication_error"


class ForbiddenError(ClashClientError):
    code = "forbidden"


class NotFoundError(ClashClientError):
    code = "not_found"


class RateLimitError(ClashClientError):
    code = "rate_limited"
    retryable = True

    @property
    def retry_after_seconds(self) -> Optional[float]:
        if self.rate_limit is None:
            return None
        return self.rate_limit.retry_after_seconds


class UpstreamServerError(ClashClientError):
    code = "upstream_server_error"
    retryable = True


class UnexpectedStatusError(ClashClientError):
    code = "unexpected_status"


# Common aliases make the public surface easy to discover without creating a
# second exception hierarchy.
ClashApiError = ClashClientError
ClashAPIError = ClashClientError
ClashClientException = ClashClientError


def normalize_tag(tag: str) -> str:
    """Return a canonical tag without ``#`` or URL encoding.

    Inputs such as ``TAG``, ``#TAG`` and ``%23TAG`` identify the same player
    or clan.  The decoded value is validated before it is used in a request
    path so a malformed tag cannot inject a path segment or query string.
    """

    if not isinstance(tag, str):
        raise InvalidTagError("Clash tag must be a string.")

    candidate = tag.strip()
    # Decode at most twice.  One pass handles the supported %23 form; the
    # second pass also handles an accidental double encoding without
    # accepting an unbounded encoded input.
    for _ in range(2):
        decoded = unquote(candidate)
        if decoded == candidate:
            break
        candidate = decoded

    candidate = candidate.strip()
    while candidate.startswith("#"):
        candidate = candidate[1:]

    normalized = candidate.upper()
    if not normalized or not _TAG_PATTERN.fullmatch(normalized):
        raise InvalidTagError("Clash tag is missing or has an invalid format.")
    return normalized


def encode_tag(tag: str) -> str:
    """Return a normalized tag in the official API path form, e.g. ``%23TAG``."""

    normalized = normalize_tag(tag)
    return quote(f"#{normalized}", safe="")


# Alternate descriptive names are useful to callers and remain one
# implementation, so there is no possibility of the normalizers drifting.
normalize_clash_tag = normalize_tag
encoded_tag = encode_tag


def _as_finite_float(value: Any, *, name: str, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{name} must be a finite positive number.")
    result = float(value)
    if not math.isfinite(result) or result <= 0 or result > maximum:
        raise ConfigurationError(f"{name} must be between 0 and {maximum} seconds.")
    return result


def _validate_timeout(timeout: Any) -> Any:
    if isinstance(timeout, (tuple, list)):
        if len(timeout) != 2:
            raise ConfigurationError("timeout must contain connect and read values.")
        return tuple(
            _as_finite_float(value, name="timeout", maximum=MAX_TIMEOUT)
            for value in timeout
        )
    return _as_finite_float(timeout, name="timeout", maximum=MAX_TIMEOUT)


def _utc_now(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime):
        raise ConfigurationError("clock must return a datetime.")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _isoformat_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _header_value(headers: Any, *wanted_names: str) -> Optional[str]:
    if not isinstance(headers, Mapping):
        return None
    wanted = {name.lower() for name in wanted_names}
    for key, value in headers.items():
        if str(key).lower() in wanted and value is not None:
            return str(value).strip()
    return None


def _safe_int(value: Optional[str]) -> Optional[int]:
    if value is None or not re.fullmatch(r"\d+", value):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_seconds(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    if not re.fullmatch(r"\d+(?:\.\d+)?", value):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds) or seconds < 0 or seconds > 86400:
        return None
    return seconds


@dataclass(frozen=True)
class RateLimitMetadata(Mapping[str, Any]):
    """Safe, selected rate-limit information from response headers."""

    limit: Optional[int] = None
    remaining: Optional[int] = None
    reset_epoch_seconds: Optional[int] = None
    retry_after_seconds: Optional[float] = None

    @property
    def reset(self) -> Optional[int]:
        return self.reset_epoch_seconds

    @property
    def retry_after(self) -> Optional[float]:
        return self.retry_after_seconds

    def as_dict(self) -> Dict[str, Any]:
        return {
            "limit": self.limit,
            "remaining": self.remaining,
            "reset": self.reset_epoch_seconds,
            "reset_epoch_seconds": self.reset_epoch_seconds,
            "retry_after": self.retry_after_seconds,
            "retry_after_seconds": self.retry_after_seconds,
        }

    to_dict = as_dict

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def __iter__(self):
        return iter(self.as_dict())

    def __len__(self) -> int:
        return len(self.as_dict())


def _rate_limit_metadata(headers: Any, *, now: datetime) -> RateLimitMetadata:
    retry_after_value = _header_value(headers, "retry-after")
    retry_after = _safe_seconds(retry_after_value)
    if retry_after is None and retry_after_value:
        try:
            retry_date = parsedate_to_datetime(retry_after_value)
            if retry_date.tzinfo is None:
                retry_date = retry_date.replace(tzinfo=timezone.utc)
            retry_after = max(
                0.0,
                (retry_date.astimezone(timezone.utc) - now).total_seconds(),
            )
        except (TypeError, ValueError, OverflowError):
            retry_after = None

    return RateLimitMetadata(
        limit=_safe_int(
            _header_value(headers, "x-ratelimit-limit", "ratelimit-limit")
        ),
        remaining=_safe_int(
            _header_value(
                headers,
                "x-ratelimit-remaining",
                "ratelimit-remaining",
            )
        ),
        reset_epoch_seconds=_safe_int(
            _header_value(
                headers,
                "x-ratelimit-reset",
                "ratelimit-reset",
            )
        ),
        retry_after_seconds=retry_after,
    )


@dataclass(frozen=True)
class ResponseMetadata(Mapping[str, Any]):
    """Envelope metadata that is safe to serialize for a route response."""

    source: str
    fetched_at: str
    is_stale: bool = False
    status_code: Optional[int] = None
    attempts: int = 1
    endpoint: str = ""
    rate_limit: Optional[RateLimitMetadata] = None
    empty: bool = False
    stale_reason: Optional[str] = None
    error_code: Optional[str] = None

    @property
    def stale(self) -> bool:
        return self.is_stale

    @property
    def data_status(self) -> str:
        if self.is_stale:
            return "stale"
        if self.empty:
            return "empty"
        return "fresh"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "fetched_at": self.fetched_at,
            "is_stale": self.is_stale,
            "stale": self.is_stale,
            "data_status": self.data_status,
            "status_code": self.status_code,
            "attempts": self.attempts,
            "endpoint": self.endpoint,
            "rate_limit": (
                self.rate_limit.as_dict() if self.rate_limit is not None else None
            ),
            "empty": self.empty,
            "stale_reason": self.stale_reason,
            "error_code": self.error_code,
        }

    to_dict = as_dict

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def __iter__(self):
        return iter(self.as_dict())

    def __len__(self) -> int:
        return len(self.as_dict())


@dataclass(frozen=True)
class ClashResponse(Generic[T], Mapping[str, Any]):
    """An upstream JSON payload plus safe freshness and transport metadata."""

    data: T
    metadata: ResponseMetadata

    @property
    def payload(self) -> T:
        return self.data

    @property
    def body(self) -> T:
        return self.data

    @property
    def source(self) -> str:
        return self.metadata.source

    @property
    def fetched_at(self) -> str:
        return self.metadata.fetched_at

    @property
    def is_stale(self) -> bool:
        return self.metadata.is_stale

    @property
    def stale(self) -> bool:
        return self.metadata.is_stale

    @property
    def rate_limit(self) -> Optional[RateLimitMetadata]:
        return self.metadata.rate_limit

    def as_dict(self) -> Dict[str, Any]:
        return {"data": self.data, "metadata": self.metadata.as_dict()}

    to_dict = as_dict

    def __getitem__(self, key: str) -> Any:
        if key == "data":
            return self.data
        if key == "payload":
            return self.data
        if key == "metadata":
            return self.metadata
        if key == "source":
            return self.source
        if key == "fetched_at":
            return self.fetched_at
        if key in {"is_stale", "stale"}:
            return self.is_stale
        if isinstance(self.data, Mapping):
            return self.data[key]
        raise KeyError(key)

    def __contains__(self, key: object) -> bool:
        if key in {"data", "payload", "metadata", "source", "fetched_at", "is_stale", "stale"}:
            return True
        return isinstance(self.data, Mapping) and key in self.data

    def __iter__(self):
        return iter(("data", "metadata"))

    def __len__(self) -> int:
        return 2


Requester = Callable[..., Any]


class ClashRoyaleClient:
    """Configurable, mock-friendly client for official Clash Royale GET routes."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: Any = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
        max_backoff: float = 8.0,
        stale_if_error: bool = True,
        requester: Optional[Requester] = None,
        request_get: Optional[Requester] = None,
        transport: Optional[Requester] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        source: str = DEFAULT_SOURCE,
    ) -> None:
        configured_key = (
            os.environ.get("CLASH_ROYALE_API_KEY") if api_key is None else api_key
        )
        if not isinstance(configured_key, str) or not configured_key.strip():
            raise ConfigurationError("CLASH_ROYALE_API_KEY is not configured.")
        self._api_key = configured_key.strip()

        if not isinstance(base_url, str):
            raise ConfigurationError("base_url must be a URL string.")
        parsed_base = urlsplit(base_url.strip())
        if (
            parsed_base.scheme not in {"http", "https"}
            or not parsed_base.netloc
            or parsed_base.username is not None
            or parsed_base.password is not None
            or parsed_base.query
            or parsed_base.fragment
        ):
            raise ConfigurationError("base_url must be an absolute HTTP(S) URL without credentials.")
        self.base_url = base_url.strip().rstrip("/")

        self.timeout = _validate_timeout(timeout)

        if isinstance(max_retries, bool) or not isinstance(max_retries, int):
            raise ConfigurationError("max_retries must be a bounded non-negative integer.")
        if max_retries < 0 or max_retries > MAX_RETRIES:
            raise ConfigurationError(
                f"max_retries must be between 0 and {MAX_RETRIES}."
            )
        self.max_retries = max_retries

        if isinstance(backoff_factor, bool) or not isinstance(backoff_factor, (int, float)):
            raise ConfigurationError("backoff_factor must be a finite non-negative number.")
        self.backoff_factor = float(backoff_factor)
        if (
            not math.isfinite(self.backoff_factor)
            or self.backoff_factor < 0
            or self.backoff_factor > MAX_BACKOFF
        ):
            raise ConfigurationError(
                f"backoff_factor must be between 0 and {MAX_BACKOFF} seconds."
            )

        if isinstance(max_backoff, bool) or not isinstance(max_backoff, (int, float)):
            raise ConfigurationError("max_backoff must be a finite non-negative number.")
        self.max_backoff = float(max_backoff)
        if not math.isfinite(self.max_backoff) or self.max_backoff < 0 or self.max_backoff > MAX_BACKOFF:
            raise ConfigurationError(
                f"max_backoff must be between 0 and {MAX_BACKOFF} seconds."
            )

        if not isinstance(stale_if_error, bool):
            raise ConfigurationError("stale_if_error must be a boolean.")
        self.stale_if_error = stale_if_error

        supplied_requesters = [
            candidate
            for candidate in (requester, request_get, transport)
            if candidate is not None
        ]
        if len(supplied_requesters) > 1:
            raise ConfigurationError("Provide only one HTTP requester.")
        if supplied_requesters and not callable(supplied_requesters[0]):
            raise ConfigurationError("HTTP requester must be callable.")
        self._requester = supplied_requesters[0] if supplied_requesters else None

        self._sleep = time.sleep if sleep_fn is None else sleep_fn
        if not callable(self._sleep):
            raise ConfigurationError("sleep_fn must be callable.")
        self._clock = (
            (lambda: datetime.now(timezone.utc)) if clock is None else clock
        )
        if not callable(self._clock):
            raise ConfigurationError("clock must be callable.")

        if not isinstance(source, str) or not source.strip():
            raise ConfigurationError("source must be a non-empty string.")
        self.source = source.strip()
        self._cache: Dict[str, ClashResponse[Any]] = {}

    def __repr__(self) -> str:
        # Never let an accidental debug print expose the server-side key.
        return (
            f"{self.__class__.__name__}(base_url={self.base_url!r}, "
            f"timeout={self.timeout!r}, max_retries={self.max_retries!r})"
        )

    def get_clan(self, clan_tag: str) -> ClashResponse[Mapping[str, Any]]:
        return self._get_tagged("clans", clan_tag)

    def get_members(self, clan_tag: str) -> ClashResponse[Mapping[str, Any]]:
        return self._get_tagged("clans", clan_tag, "/members")

    def get_current_river_race(self, clan_tag: str) -> ClashResponse[Mapping[str, Any]]:
        return self._get_tagged("clans", clan_tag, "/currentriverrace")

    def get_river_race_log(
        self,
        clan_tag: str,
        limit: Optional[int] = None,
    ) -> ClashResponse[Mapping[str, Any]]:
        normalized = normalize_tag(clan_tag)
        path = f"/clans/{quote(f'#{normalized}', safe='')}/riverracelog"
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
                raise InvalidRequestError("river race log limit must be an integer from 1 to 100.")
            path = f"{path}?limit={limit}"
        return self._request(path, cache_key=path)

    def get_player(self, player_tag: str) -> ClashResponse[Mapping[str, Any]]:
        return self._get_tagged("players", player_tag)

    def get_player_battlelog(self, player_tag: str) -> ClashResponse[Any]:
        return self._get_tagged("players", player_tag, "/battlelog")

    def _get_tagged(self, resource: str, tag: str, suffix: str = "") -> ClashResponse[Any]:
        normalized = normalize_tag(tag)
        path = f"/{resource}/{quote(f'#{normalized}', safe='')}{suffix}"
        return self._request(path, cache_key=path)

    def _request(self, path: str, *, cache_key: str) -> ClashResponse[Any]:
        url = f"{self.base_url}{path}"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        attempts = 0
        last_rate_limit: Optional[RateLimitMetadata] = None

        while True:
            attempts += 1
            try:
                requester = self._requester or requests.get
                response = requester(url, headers=headers, timeout=self.timeout)
            except (requests.Timeout, BuiltinTimeoutError, TimeoutError):
                # BuiltinTimeoutError is included for test doubles and
                # standard-library transports, while requests.Timeout covers
                # requests' exception hierarchy.
                error = TimeoutError(
                    "Clash Royale API request timed out.",
                    endpoint=path,
                    attempts=attempts,
                    rate_limit=last_rate_limit,
                )
                if attempts <= self.max_retries:
                    self._sleep_before_retry(attempts, last_rate_limit)
                    continue
                return self._return_stale_or_raise(
                    cache_key,
                    error,
                    attempts=attempts,
                    rate_limit=last_rate_limit,
                )
            except requests.ConnectionError:
                error = TransportError(
                    "Clash Royale API network request failed.",
                    endpoint=path,
                    attempts=attempts,
                    rate_limit=last_rate_limit,
                )
                if attempts <= self.max_retries:
                    self._sleep_before_retry(attempts, last_rate_limit)
                    continue
                return self._return_stale_or_raise(
                    cache_key,
                    error,
                    attempts=attempts,
                    rate_limit=last_rate_limit,
                )
            except requests.RequestException:
                error = TransportError(
                    "Clash Royale API request failed.",
                    endpoint=path,
                    attempts=attempts,
                    rate_limit=last_rate_limit,
                )
                return self._return_stale_or_raise(
                    cache_key,
                    error,
                    attempts=attempts,
                    rate_limit=last_rate_limit,
                )
            except Exception:
                # Do not propagate arbitrary transport exception text: test
                # doubles and custom adapters may accidentally include a key.
                error = TransportError(
                    "Clash Royale API transport failed.",
                    endpoint=path,
                    attempts=attempts,
                    rate_limit=last_rate_limit,
                )
                return self._return_stale_or_raise(
                    cache_key,
                    error,
                    attempts=attempts,
                    rate_limit=last_rate_limit,
                )

            try:
                status_code = self._status_code(response, path=path, attempts=attempts)
            except ClashClientError as error:
                return self._return_stale_or_raise(
                    cache_key,
                    error,
                    attempts=attempts,
                    rate_limit=last_rate_limit,
                )
            now = _utc_now(self._clock)
            last_rate_limit = _rate_limit_metadata(
                getattr(response, "headers", None),
                now=now,
            )

            if self._is_retryable_status(status_code) and attempts <= self.max_retries:
                self._sleep_before_retry(attempts, last_rate_limit)
                continue

            if not 200 <= status_code < 300:
                error = self._map_status(
                    status_code,
                    endpoint=path,
                    attempts=attempts,
                    rate_limit=last_rate_limit,
                )
                return self._return_stale_or_raise(
                    cache_key,
                    error,
                    attempts=attempts,
                    rate_limit=last_rate_limit,
                )

            try:
                payload, is_empty = self._decode_json(response, status_code=status_code, path=path, attempts=attempts)
            except ClashClientError as error:
                return self._return_stale_or_raise(
                    cache_key,
                    error,
                    attempts=attempts,
                    rate_limit=last_rate_limit,
                )

            metadata = ResponseMetadata(
                source=self.source,
                fetched_at=_isoformat_utc(now),
                is_stale=False,
                status_code=status_code,
                attempts=attempts,
                endpoint=path,
                rate_limit=last_rate_limit,
                empty=is_empty,
            )
            result = ClashResponse(data=payload, metadata=metadata)
            # Empty JSON is explicitly surfaced but is not used as a stale
            # fallback later, because it does not represent a known-good data
            # snapshot.
            if not is_empty:
                self._cache[cache_key] = self._clone_response(result)
            return result

    @staticmethod
    def _status_code(response: Any, *, path: str, attempts: int) -> int:
        raw_status = getattr(response, "status_code", None)
        try:
            status_code = int(raw_status)
        except Exception:
            raise InvalidResponseError(
                "Clash Royale API returned an invalid HTTP response.",
                endpoint=path,
                attempts=attempts,
            )
        if status_code < 100 or status_code > 599:
            raise InvalidResponseError(
                "Clash Royale API returned an invalid HTTP response.",
                endpoint=path,
                attempts=attempts,
            )
        return status_code

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return status_code in _RETRYABLE_STATUSES or 500 <= status_code <= 599

    def _sleep_before_retry(
        self,
        attempt: int,
        rate_limit: Optional[RateLimitMetadata],
    ) -> None:
        retry_after = rate_limit.retry_after_seconds if rate_limit is not None else None
        if retry_after is None:
            delay = self.backoff_factor * (2 ** max(0, attempt - 1))
        else:
            delay = retry_after
        delay = min(max(0.0, delay), self.max_backoff)
        if delay > 0:
            self._sleep(delay)

    @staticmethod
    def _map_status(
        status_code: int,
        *,
        endpoint: str,
        attempts: int,
        rate_limit: Optional[RateLimitMetadata],
    ) -> ClashClientError:
        common = {
            "endpoint": endpoint,
            "status_code": status_code,
            "attempts": attempts,
            "rate_limit": rate_limit,
        }
        if status_code == 400:
            return BadRequestError("Clash Royale API rejected the request.", **common)
        if status_code == 401:
            return AuthenticationError("Clash Royale API authentication failed.", **common)
        if status_code == 403:
            return ForbiddenError("Clash Royale API access was forbidden.", **common)
        if status_code == 404:
            return NotFoundError("Clash Royale resource was not found.", **common)
        if status_code == 429:
            return RateLimitError("Clash Royale API rate limit reached.", **common)
        if 500 <= status_code <= 599:
            return UpstreamServerError(
                "Clash Royale API service is temporarily unavailable.",
                **common,
            )
        return UnexpectedStatusError(
            "Clash Royale API returned an unexpected HTTP status.",
            **common,
        )

    @staticmethod
    def _decode_json(
        response: Any,
        *,
        status_code: int,
        path: str,
        attempts: int,
    ) -> tuple[Any, bool]:
        content = getattr(response, "content", None)
        if isinstance(content, (bytes, bytearray)) and not content.strip():
            raise EmptyResponseError(
                "Clash Royale API returned an empty response.",
                endpoint=path,
                status_code=status_code,
                attempts=attempts,
            )
        if isinstance(content, str) and not content.strip():
            raise EmptyResponseError(
                "Clash Royale API returned an empty response.",
                endpoint=path,
                status_code=status_code,
                attempts=attempts,
            )

        json_loader = getattr(response, "json", None)
        if not callable(json_loader):
            raise InvalidResponseError(
                "Clash Royale API response has no JSON body.",
                endpoint=path,
                status_code=status_code,
                attempts=attempts,
            )
        try:
            payload = json_loader()
        except Exception:
            raise ResponseDecodeError(
                "Clash Royale API returned invalid JSON.",
                endpoint=path,
                status_code=status_code,
                attempts=attempts,
            )

        if payload is None:
            raise EmptyResponseError(
                "Clash Royale API returned an empty response.",
                endpoint=path,
                status_code=status_code,
                attempts=attempts,
            )
        if not isinstance(payload, (dict, list)):
            raise InvalidResponseError(
                "Clash Royale API returned an unsupported JSON shape.",
                endpoint=path,
                status_code=status_code,
                attempts=attempts,
            )
        return payload, len(payload) == 0

    def _return_stale_or_raise(
        self,
        cache_key: str,
        error: ClashClientError,
        *,
        attempts: int,
        rate_limit: Optional[RateLimitMetadata],
    ) -> ClashResponse[Any]:
        cached = self._cache.get(cache_key)
        stale_safe_error = error.retryable or error.code in {
            "empty_response",
            "invalid_json",
            "invalid_response",
        }
        if self.stale_if_error and stale_safe_error and cached is not None:
            cached_metadata = cached.metadata
            stale_metadata = ResponseMetadata(
                source=cached_metadata.source,
                fetched_at=cached_metadata.fetched_at,
                is_stale=True,
                status_code=error.status_code,
                attempts=attempts,
                endpoint=cached_metadata.endpoint,
                rate_limit=rate_limit or cached_metadata.rate_limit,
                empty=cached_metadata.empty,
                stale_reason=error.code,
                error_code=error.code,
            )
            return ClashResponse(
                data=deepcopy(cached.data),
                metadata=stale_metadata,
            )
        raise error

    @staticmethod
    def _clone_response(response: ClashResponse[Any]) -> ClashResponse[Any]:
        return ClashResponse(
            data=deepcopy(response.data),
            metadata=response.metadata,
        )


# Descriptive aliases keep imports ergonomic while all callers share the same
# implementation and cache/error behavior.
ClashClient = ClashRoyaleClient
ClashAPIClient = ClashRoyaleClient
ClashRoyaleAPIClient = ClashRoyaleClient


# Module-level helpers mirror the requested endpoint names for simple route
# code.  They create a client per call; stateful callers should instantiate
# ClashRoyaleClient directly so the in-memory stale fallback can be reused.
def get_clan(clan_tag: str, **client_options: Any) -> ClashResponse[Any]:
    return ClashRoyaleClient(**client_options).get_clan(clan_tag)


def get_members(clan_tag: str, **client_options: Any) -> ClashResponse[Any]:
    return ClashRoyaleClient(**client_options).get_members(clan_tag)


def get_current_river_race(clan_tag: str, **client_options: Any) -> ClashResponse[Any]:
    return ClashRoyaleClient(**client_options).get_current_river_race(clan_tag)


def get_river_race_log(
    clan_tag: str,
    limit: Optional[int] = None,
    **client_options: Any,
) -> ClashResponse[Any]:
    return ClashRoyaleClient(**client_options).get_river_race_log(clan_tag, limit)


def get_player(player_tag: str, **client_options: Any) -> ClashResponse[Any]:
    return ClashRoyaleClient(**client_options).get_player(player_tag)


def get_player_battlelog(player_tag: str, **client_options: Any) -> ClashResponse[Any]:
    return ClashRoyaleClient(**client_options).get_player_battlelog(player_tag)


__all__ = [
    "AuthenticationError",
    "BadRequestError",
    "ClashAPIError",
    "ClashAPIClient",
    "ClashClient",
    "ClashApiError",
    "ClashClientError",
    "ClashClientException",
    "ClashResponse",
    "ClashRoyaleClient",
    "ClashRoyaleAPIClient",
    "ConfigurationError",
    "DEFAULT_BASE_URL",
    "DEFAULT_SOURCE",
    "DEFAULT_TIMEOUT",
    "EmptyResponseError",
    "ForbiddenError",
    "InvalidRequestError",
    "InvalidResponseError",
    "InvalidTagError",
    "NotFoundError",
    "RateLimitError",
    "RateLimitMetadata",
    "ROYAL_API_BASE_URL",
    "ResponseDecodeError",
    "ResponseMetadata",
    "TimeoutError",
    "TransportError",
    "UnexpectedStatusError",
    "UpstreamServerError",
    "encode_tag",
    "encoded_tag",
    "get_clan",
    "get_current_river_race",
    "get_members",
    "get_player",
    "get_player_battlelog",
    "get_river_race_log",
    "normalize_clash_tag",
    "normalize_tag",
]
