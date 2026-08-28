"""Small, server-only Discord incoming-webhook client for war alerts.

The module deliberately has no route or frontend dependencies.  It accepts a
pre-built, allow-listed payload and never includes the configured webhook URL,
request headers, response body, or exception text in its result.  The HTTP
call is injectable so all retry and redaction behavior can be tested without a
network connection.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
import re
import time
from typing import Any, Callable, Dict, Mapping, Optional
from urllib.parse import urlsplit

import requests


DISCORD_WEBHOOK_URL_ENV = "DISCORD_WAR_WEBHOOK_URL"
DISCORD_WAR_WEBHOOK_URL_ENV = DISCORD_WEBHOOK_URL_ENV
DISCORD_CHANNEL = "discord"
DEFAULT_DISCORD_TIMEOUT = 10.0
DEFAULT_DISCORD_MAX_RETRIES = 2
DEFAULT_DISCORD_BACKOFF_SECONDS = 0.5
DEFAULT_DISCORD_MAX_BACKOFF_SECONDS = 5.0

STATUS_DUEL_FIRST_LIKELY = "duel_first_likely"
STATUS_SOLO_START_OBSERVED = "solo_start_observed"
ALERTABLE_STATUSES = frozenset(
    {
        STATUS_DUEL_FIRST_LIKELY,
        STATUS_SOLO_START_OBSERVED,
    }
)
_CONFIDENCES = frozenset({"unknown", "low", "medium", "high"})
_TAG_PATTERN = re.compile(r"[A-Z0-9]{1,32}\Z")
_DISCORD_WEBHOOK_HOSTS = frozenset(
    {
        "discord.com",
        "discordapp.com",
        "canary.discord.com",
        "canary.discordapp.com",
        "ptb.discord.com",
        "ptb.discordapp.com",
    }
)
_MAX_WEBHOOK_URL_LENGTH = 2048
_MAX_DISPLAY_TEXT_LENGTH = 160
_MAX_RACE_DAY_KEY_LENGTH = 256


class DiscordWebhookConfigurationError(RuntimeError):
    """Safe configuration error without retaining a URL or secret."""

    code = "configuration_error"

    def __init__(self) -> None:
        super().__init__("Discord webhook configuration is invalid.")


def _safe_timestamp(value: Any = None) -> str:
    if value is None:
        parsed = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            parsed = datetime.now(timezone.utc)
    else:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _clock_timestamp(clock: Optional[Callable[[], Any]]) -> str:
    if clock is None:
        return _safe_timestamp()
    try:
        return _safe_timestamp(clock())
    except Exception:
        return _safe_timestamp()


def validate_discord_webhook_url(value: Any) -> str:
    """Validate a webhook URL without returning it in an error message."""

    if not isinstance(value, str):
        raise DiscordWebhookConfigurationError()
    url = value.strip()
    if (
        not url
        or len(url) > _MAX_WEBHOOK_URL_LENGTH
        or any(character.isspace() or ord(character) < 32 for character in url)
    ):
        raise DiscordWebhookConfigurationError()
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
    except ValueError:
        raise DiscordWebhookConfigurationError() from None
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or not parsed.path
        or hostname not in _DISCORD_WEBHOOK_HOSTS
        or not parsed.path.startswith("/api/webhooks/")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise DiscordWebhookConfigurationError()
    return url


def configured_discord_webhook_url() -> Optional[str]:
    """Return the valid server-side URL, or ``None`` when disabled/invalid."""

    value = os.environ.get(DISCORD_WEBHOOK_URL_ENV, "").strip()
    if not value:
        return None
    try:
        return validate_discord_webhook_url(value)
    except DiscordWebhookConfigurationError:
        return None


def is_alertable_event(event: Any) -> bool:
    """Return true only for a new T07 event with a usable counter."""

    if not isinstance(event, Mapping):
        return False
    if event.get("new_event") is False:
        return False
    event_key = event.get("event_key")
    if not isinstance(event_key, str) or not event_key.strip():
        return False
    status = event.get("event_type")
    if status not in ALERTABLE_STATUSES:
        status = event.get("status")
    if status not in ALERTABLE_STATUSES:
        return False
    current_count = event.get(
        "observed_decks_used_today",
        event.get("current_decks_used_today"),
    )
    if isinstance(current_count, bool):
        return False
    if isinstance(current_count, str) and re.fullmatch(r"\d+", current_count.strip()):
        current_count = int(current_count.strip())
    return isinstance(current_count, int) and current_count > 0


def _display_text(value: Any, fallback: str, *, limit: int = _MAX_DISPLAY_TEXT_LENGTH) -> str:
    if value is None:
        text = fallback
    else:
        text = str(value).strip()
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    text = " ".join(text.split())
    text = text.replace("`", "'")
    return (text or fallback)[:limit].rstrip() or fallback


def _display_tag(value: Any, field: str) -> str:
    raw = _display_text(value, "", limit=32).lstrip("#").upper()
    if not _TAG_PATTERN.fullmatch(raw):
        raise ValueError(f"{field} must be a valid player or clan tag.")
    return f"#{raw}"


def _event_details(event: Mapping[str, Any]) -> Mapping[str, Any]:
    details = event.get("details")
    return details if isinstance(details, Mapping) else {}


def _required_event_timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Alertable event observed_at is required.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("Alertable event observed_at is invalid.") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_discord_payload(
    event: Mapping[str, Any],
    *,
    clan_name: Any = None,
    player_name: Any = None,
    player_tag: Any = None,
) -> Dict[str, Any]:
    """Build a small, allow-listed private-channel payload for one T07 event."""

    if not is_alertable_event(event):
        raise ValueError("Only alertable T07 events can be sent to Discord.")

    status = event.get("event_type")
    if status not in ALERTABLE_STATUSES:
        status = event.get("status")
    confidence = event.get("confidence", "unknown")
    if confidence not in _CONFIDENCES:
        confidence = "unknown"

    current_count = event.get(
        "observed_decks_used_today",
        event.get("current_decks_used_today"),
    )
    if isinstance(current_count, str):
        current_count = int(current_count.strip())

    observed_at = _required_event_timestamp(event.get("observed_at"))

    details = _event_details(event)
    race_day_key = details.get("race_day_key", event.get("race_day_key"))
    race_day_key = _display_text(
        race_day_key,
        "onbekende race-dag",
        limit=_MAX_RACE_DAY_KEY_LENGTH,
    )
    safe_player_tag = _display_tag(
        player_tag if player_tag is not None else event.get("player_tag"),
        "player_tag",
    )
    safe_clan_tag = _display_tag(event.get("clan_tag"), "clan_tag")
    safe_player_name = _display_text(
        player_name if player_name is not None else event.get("player_name"),
        safe_player_tag,
    )
    safe_clan_name = _display_text(clan_name, safe_clan_tag)

    content = "\n".join(
        (
            "⚔️ Duel-eerst controle",
            f"Speler: {safe_player_name} ({safe_player_tag})",
            f"Clan: {safe_clan_name} ({safe_clan_tag})",
            f"Status: {status}",
            f"Confidence: {confidence}",
            f"Teller: {current_count}",
            f"Context: {race_day_key}",
            f"Observed at: {observed_at}",
        )
    )
    return {
        "content": content,
        "allowed_mentions": {"parse": []},
    }


def _validate_request_options(
    *,
    timeout: float,
    max_retries: int,
    backoff_factor: float,
    max_backoff: float,
) -> None:
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
        or timeout <= 0
    ):
        raise ValueError("timeout must be a positive finite number.")
    if (
        isinstance(max_retries, bool)
        or not isinstance(max_retries, int)
        or not 0 <= max_retries <= 3
    ):
        raise ValueError("max_retries must be between 0 and 3.")
    for name, value in (
        ("backoff_factor", backoff_factor),
        ("max_backoff", max_backoff),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value < 0
        ):
            raise ValueError(f"{name} must be a finite non-negative number.")
    if backoff_factor > max_backoff:
        raise ValueError("backoff_factor must not exceed max_backoff.")


def _retry_delay(
    response: Any,
    *,
    attempt: int,
    backoff_factor: float,
    max_backoff: float,
) -> float:
    retry_after = None
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            raw_retry_after = headers.get("Retry-After")
            if raw_retry_after is None and hasattr(headers, "items"):
                raw_retry_after = next(
                    (
                        value
                        for key, value in headers.items()
                        if str(key).lower() == "retry-after"
                    ),
                    None,
                )
            if raw_retry_after is not None:
                retry_after = float(raw_retry_after)
        except (TypeError, ValueError, OverflowError):
            retry_after = None
    if retry_after is not None and math.isfinite(retry_after):
        return min(max_backoff, max(0.0, retry_after))
    return min(max_backoff, backoff_factor * (2 ** max(0, attempt - 1)))


def _result(
    status: str,
    *,
    attempts: int,
    response_code: Optional[int] = None,
    error: Optional[str] = None,
    sent_at: Optional[str] = None,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "ok": status in {"sent", "disabled"},
        "status": status,
        "attempts": attempts,
    }
    if response_code is not None:
        result["response_code"] = response_code
    if error is not None:
        result["error"] = error
    if sent_at is not None:
        result["sent_at"] = sent_at
    return result


def send_discord_webhook(
    payload: Mapping[str, Any],
    *,
    webhook_url: Optional[str] = None,
    http_post: Optional[Callable[..., Any]] = None,
    timeout: float = DEFAULT_DISCORD_TIMEOUT,
    max_retries: int = DEFAULT_DISCORD_MAX_RETRIES,
    backoff_factor: float = DEFAULT_DISCORD_BACKOFF_SECONDS,
    max_backoff: float = DEFAULT_DISCORD_MAX_BACKOFF_SECONDS,
    sleep: Optional[Callable[[float], None]] = None,
    clock: Optional[Callable[[], Any]] = None,
) -> Dict[str, Any]:
    """POST a payload with bounded Discord-specific retry behavior."""

    _validate_request_options(
        timeout=timeout,
        max_retries=max_retries,
        backoff_factor=backoff_factor,
        max_backoff=max_backoff,
    )
    if not isinstance(payload, Mapping):
        return _result(
            "failed",
            attempts=0,
            error="invalid_payload",
            sent_at=_clock_timestamp(clock),
        )

    selected_url = configured_discord_webhook_url() if webhook_url is None else webhook_url
    if not selected_url:
        return _result("disabled", attempts=0)
    try:
        safe_url = validate_discord_webhook_url(selected_url)
    except DiscordWebhookConfigurationError:
        return _result(
            "disabled",
            attempts=0,
            error="configuration_error",
        )

    try:
        json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError):
        return _result(
            "failed",
            attempts=0,
            error="invalid_payload",
            sent_at=_clock_timestamp(clock),
        )

    request_post = http_post or requests.post
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Brabant-Royale-War-Monitor/1",
    }
    sleep_fn = sleep or time.sleep

    for attempt in range(1, max_retries + 2):
        response = None
        try:
            response = request_post(
                safe_url,
                json=dict(payload),
                headers=headers,
                timeout=timeout,
            )
            status_code = int(response.status_code)
        except requests.exceptions.Timeout:
            status_code = None
            error_code = "timeout"
        except TimeoutError:
            status_code = None
            error_code = "timeout"
        except requests.exceptions.RequestException:
            status_code = None
            error_code = "transport_error"
        except (TypeError, ValueError, AttributeError):
            status_code = None
            error_code = "invalid_response"
        except Exception:
            status_code = None
            error_code = "transport_error"
        else:
            if 200 <= status_code < 300:
                return _result(
                    "sent",
                    attempts=attempt,
                    response_code=status_code,
                    sent_at=_clock_timestamp(clock),
                )
            if status_code == 400:
                return _result(
                    "failed",
                    attempts=attempt,
                    response_code=status_code,
                    error="bad_request",
                    sent_at=_clock_timestamp(clock),
                )
            if status_code == 429:
                error_code = "rate_limited"
            elif 500 <= status_code <= 599:
                error_code = "upstream_server_error"
            else:
                return _result(
                    "failed",
                    attempts=attempt,
                    response_code=status_code,
                    error="unexpected_status",
                    sent_at=_clock_timestamp(clock),
                )

        if attempt <= max_retries:
            delay = (
                _retry_delay(
                    response,
                    attempt=attempt,
                    backoff_factor=backoff_factor,
                    max_backoff=max_backoff,
                )
                if response is not None
                else min(max_backoff, backoff_factor * (2 ** max(0, attempt - 1)))
            )
            if delay > 0:
                sleep_fn(delay)
            continue

        return _result(
            "failed",
            attempts=attempt,
            response_code=status_code,
            error=error_code,
            sent_at=_clock_timestamp(clock),
        )

    return _result(
        "failed",
        attempts=max_retries + 1,
        error="transport_error",
        sent_at=_clock_timestamp(clock),
    )


# Descriptive aliases keep the integration seam easy to discover.
def post_discord_webhook(
    webhook_url: str,
    payload: Mapping[str, Any],
    **kwargs: Any,
) -> Dict[str, Any]:
    """Compatibility wrapper with the conventional URL-first argument order."""

    return send_discord_webhook(payload, webhook_url=webhook_url, **kwargs)


get_configured_webhook_url = configured_discord_webhook_url


__all__ = [
    "ALERTABLE_STATUSES",
    "DEFAULT_DISCORD_BACKOFF_SECONDS",
    "DEFAULT_DISCORD_MAX_BACKOFF_SECONDS",
    "DEFAULT_DISCORD_MAX_RETRIES",
    "DEFAULT_DISCORD_TIMEOUT",
    "DISCORD_CHANNEL",
    "DISCORD_WEBHOOK_URL_ENV",
    "DISCORD_WAR_WEBHOOK_URL_ENV",
    "DiscordWebhookConfigurationError",
    "build_discord_payload",
    "configured_discord_webhook_url",
    "get_configured_webhook_url",
    "is_alertable_event",
    "post_discord_webhook",
    "send_discord_webhook",
    "validate_discord_webhook_url",
]
