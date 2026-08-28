"""Pure Duel-first classification for one Clash Royale player-day.

This module deliberately stops at domain logic.  It accepts already available
observations, validates and canonicalizes their identity, and returns a
deterministic result that a later route can persist or notify on.  It does not
perform I/O, consult a database, or keep state between calls.

``decksUsedToday`` is treated as a counter observation.  A ``0 -> 2`` or
``0 -> 3`` transition is therefore a likely Duel-first signal, not proof of a
first-action Duel.  Missing counters are represented by ``None`` and are never
converted to zero.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any, Optional
from urllib.parse import unquote


STATUS_NOT_STARTED = "not_started"
STATUS_DUEL_FIRST_LIKELY = "duel_first_likely"
STATUS_SOLO_START_OBSERVED = "solo_start_observed"
STATUS_UNKNOWN_START = "unknown_start"
STATUS_EXEMPT = "exempt"
STATUS_API_STALE = "api_stale"

DUEL_FIRST_STATUSES = frozenset(
    {
        STATUS_NOT_STARTED,
        STATUS_DUEL_FIRST_LIKELY,
        STATUS_SOLO_START_OBSERVED,
        STATUS_UNKNOWN_START,
        STATUS_EXEMPT,
        STATUS_API_STALE,
    }
)

_EVENT_STATUSES = frozenset({STATUS_DUEL_FIRST_LIKELY, STATUS_SOLO_START_OBSERVED})
_CONFIDENCES = frozenset({"unknown", "low", "medium", "high"})
_TAG_PATTERN = re.compile(r"^[A-Z0-9]+$")
_INTEGER_PATTERN = re.compile(r"^[+-]?\d+$")
_TIMESTAMP_FORMATS = (
    "%Y%m%dT%H%M%S.%fZ",
    "%Y%m%dT%H%M%SZ",
)


class DuelFirstValidationError(ValueError):
    """Raised when an identity, counter, or control value is unusable."""


def _validation_error(field: str, message: str) -> DuelFirstValidationError:
    return DuelFirstValidationError(f"{field} {message}.")


def normalize_tag(value: Any, *, field: str = "tag") -> str:
    """Return a canonical Clash tag or fail without retaining its raw value."""

    if not isinstance(value, str):
        raise _validation_error(field, "must be a string")

    candidate = value.strip()
    # Match the repository's T01 tag contract while keeping this module
    # independent from the HTTP client and its requests dependency.
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
        raise _validation_error(field, "has an invalid format")
    return normalized


def normalize_timestamp(value: Any, *, field: str = "timestamp") -> str:
    """Return a canonical UTC timestamp for identity or observation time."""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise _validation_error(field, "must be a valid timestamp")
        parsed = None
        for pattern in _TIMESTAMP_FORMATS:
            try:
                parsed = datetime.strptime(raw, pattern).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        if parsed is None:
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except (TypeError, ValueError, OverflowError):
                raise _validation_error(
                    field,
                    "must be a valid timestamp",
                ) from None
    else:
        raise _validation_error(field, "must be a valid timestamp")

    try:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise _validation_error(field, "must be a valid timestamp") from None


def normalize_counter(
    value: Any,
    *,
    field: str = "decks_used_today",
) -> Optional[int]:
    """Normalize a non-negative integer counter; ``None`` means missing.

    A missing value is intentionally different from zero.  Numeric strings are
    accepted because upstream and stored JSON can represent integer fields as
    strings, while booleans and floats are rejected rather than truncated.
    """

    if value is None:
        return None
    if isinstance(value, bool):
        raise _validation_error(field, "must be a non-negative integer")
    if isinstance(value, int):
        normalized = value
    elif isinstance(value, str) and _INTEGER_PATTERN.fullmatch(value.strip()):
        try:
            normalized = int(value.strip())
        except (TypeError, ValueError, OverflowError):
            raise _validation_error(
                field,
                "must be a non-negative integer",
            ) from None
    else:
        raise _validation_error(field, "must be a non-negative integer")

    if normalized < 0:
        raise _validation_error(field, "must be a non-negative integer")
    return normalized


def _normalize_identity_integer(value: Any, *, field: str) -> int:
    normalized = normalize_counter(value, field=field)
    if normalized is None:
        raise _validation_error(field, "is required")
    return normalized


@dataclass(frozen=True)
class RaceIdentity(Mapping[str, Any]):
    """Canonical identity for one race.

    The pipe separators make the requested concatenation unambiguous while
    retaining every component: clan tag, season, section, and creation time.
    """

    clan_tag: str
    season_id: int
    section_index: int
    race_created_at: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "clan_tag",
            normalize_tag(self.clan_tag, field="clan_tag"),
        )
        object.__setattr__(
            self,
            "season_id",
            _normalize_identity_integer(self.season_id, field="season_id"),
        )
        object.__setattr__(
            self,
            "section_index",
            _normalize_identity_integer(
                self.section_index,
                field="section_index",
            ),
        )
        object.__setattr__(
            self,
            "race_created_at",
            normalize_timestamp(
                self.race_created_at,
                field="race_created_at",
            ),
        )

    @property
    def race_key(self) -> str:
        return "|".join(
            (
                self.clan_tag,
                str(self.season_id),
                str(self.section_index),
                self.race_created_at,
            )
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "clan_tag": self.clan_tag,
            "season_id": self.season_id,
            "section_index": self.section_index,
            "race_created_at": self.race_created_at,
            "race_key": self.race_key,
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.as_dict())

    def __len__(self) -> int:
        return len(self.as_dict())


@dataclass(frozen=True)
class RaceDayIdentity(Mapping[str, Any]):
    """Canonical identity for one player observation day within a race."""

    race: RaceIdentity
    period_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.race, RaceIdentity):
            raise _validation_error("race", "must be a RaceIdentity")
        object.__setattr__(
            self,
            "period_index",
            _normalize_identity_integer(
                self.period_index,
                field="period_index",
            ),
        )

    @property
    def race_key(self) -> str:
        return self.race.race_key

    @property
    def race_day_key(self) -> str:
        return f"{self.race_key}|{self.period_index}"

    @property
    def clan_tag(self) -> str:
        return self.race.clan_tag

    @property
    def season_id(self) -> int:
        return self.race.season_id

    @property
    def section_index(self) -> int:
        return self.race.section_index

    @property
    def race_created_at(self) -> str:
        return self.race.race_created_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "clan_tag": self.clan_tag,
            "season_id": self.season_id,
            "section_index": self.section_index,
            "race_created_at": self.race_created_at,
            "period_index": self.period_index,
            "race_key": self.race_key,
            "race_day_key": self.race_day_key,
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.as_dict())

    def __len__(self) -> int:
        return len(self.as_dict())


def build_race_identity(
    clan_tag: Any,
    season_id: Any,
    section_index: Any,
    race_created_at: Any,
) -> RaceIdentity:
    """Validate and build the four-component race identity."""

    return RaceIdentity(
        clan_tag=normalize_tag(clan_tag, field="clan_tag"),
        season_id=_normalize_identity_integer(season_id, field="season_id"),
        section_index=_normalize_identity_integer(
            section_index,
            field="section_index",
        ),
        race_created_at=normalize_timestamp(
            race_created_at,
            field="race_created_at",
        ),
    )


def build_race_day_identity(
    race: RaceIdentity,
    period_index: Any,
) -> RaceDayIdentity:
    """Build a day identity from a validated race identity."""

    return RaceDayIdentity(race=race, period_index=period_index)


def build_race_key(
    clan_tag: Any,
    season_id: Any,
    section_index: Any,
    race_created_at: Any,
) -> str:
    """Return ``clan + season + section + race_created_at`` in canonical form."""

    return build_race_identity(
        clan_tag,
        season_id,
        section_index,
        race_created_at,
    ).race_key


def build_race_day_key(
    clan_tag: Any,
    season_id: Any,
    section_index: Any,
    race_created_at: Any,
    period_index: Any,
) -> str:
    """Return the race key extended with the canonical period index."""

    race = build_race_identity(
        clan_tag,
        season_id,
        section_index,
        race_created_at,
    )
    return build_race_day_identity(race, period_index).race_day_key


@dataclass(frozen=True)
class DuelFirstResult(Mapping[str, Any]):
    """Frozen, serializable classification output for one player-day."""

    status: str
    confidence: str
    reason: str
    observed_at: str
    player_tag: str
    identity: RaceDayIdentity
    previous_decks_used_today: Optional[int]
    current_decks_used_today: Optional[int]
    is_new_race_day: bool
    event_key: Optional[str]
    event_identity: Optional[Mapping[str, Any]]
    event_exists: bool
    new_event: bool
    event: Optional[Mapping[str, Any]]

    def __post_init__(self) -> None:
        if self.status not in DUEL_FIRST_STATUSES:
            raise _validation_error("status", "has an unsupported value")
        if self.confidence not in _CONFIDENCES:
            raise _validation_error("confidence", "has an unsupported value")
        if not isinstance(self.identity, RaceDayIdentity):
            raise _validation_error("identity", "must be a RaceDayIdentity")
        object.__setattr__(
            self,
            "player_tag",
            normalize_tag(self.player_tag, field="player_tag"),
        )

    @property
    def race_key(self) -> str:
        return self.identity.race_key

    @property
    def race_day_key(self) -> str:
        return self.identity.race_day_key

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "confidence": self.confidence,
            "reason": self.reason,
            "observed_at": self.observed_at,
            "player_tag": self.player_tag,
            "race_key": self.race_key,
            "race_day_key": self.race_day_key,
            "identity": self.identity.as_dict(),
            "previous_decks_used_today": self.previous_decks_used_today,
            "current_decks_used_today": self.current_decks_used_today,
            "is_new_race_day": self.is_new_race_day,
            "event_key": self.event_key,
            "event_identity": (
                dict(self.event_identity) if self.event_identity is not None else None
            ),
            "event_exists": self.event_exists,
            "new_event": self.new_event,
            "event": dict(self.event) if self.event is not None else None,
        }

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.as_dict())

    def __len__(self) -> int:
        return len(self.as_dict())


def _normalize_previous_day_key(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _validation_error("previous_race_day_key", "has an invalid format")
    raw = value.strip()
    parts = raw.split("|")
    if len(parts) != 5:
        raise _validation_error("previous_race_day_key", "has an invalid format")
    try:
        race = build_race_identity(*parts[:4])
        return build_race_day_identity(race, parts[4]).race_day_key
    except DuelFirstValidationError:
        raise _validation_error(
            "previous_race_day_key",
            "has an invalid format",
        ) from None


def _normalize_existing_event_keys(
    values: Optional[Iterable[Any]],
) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = (values,)
    try:
        normalized = []
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise _validation_error(
                    "existing_event_keys",
                    "contains an invalid key",
                )
            normalized.append(value.strip())
        return tuple(normalized)
    except TypeError:
        raise _validation_error(
            "existing_event_keys",
            "must be an iterable of strings",
        ) from None


def _event_key(
    identity: RaceDayIdentity,
    player_tag: str,
    event_type: str,
) -> str:
    return "|".join((identity.race_day_key, player_tag, event_type))


def _event_identity(
    identity: RaceDayIdentity,
    player_tag: str,
    event_type: str,
) -> dict[str, Any]:
    return {
        "clan_tag": identity.clan_tag,
        "season_id": identity.season_id,
        "section_index": identity.section_index,
        "race_created_at": identity.race_created_at,
        "period_index": identity.period_index,
        "player_tag": player_tag,
        "event_type": event_type,
        "race_key": identity.race_key,
        "race_day_key": identity.race_day_key,
    }


def _classify(
    previous_count: Optional[int],
    current_count: Optional[int],
    *,
    has_same_day_previous: bool,
) -> tuple[str, str, str]:
    if current_count is None:
        return (
            STATUS_UNKNOWN_START,
            "unknown",
            "No usable decksUsedToday counter was observed; missing is not treated as zero.",
        )

    if not has_same_day_previous:
        if current_count == 0:
            return (
                STATUS_NOT_STARTED,
                "high",
                "A zero decksUsedToday value was observed as the first usable measurement for this race day.",
            )
        return (
            STATUS_UNKNOWN_START,
            "low",
            "The first usable decksUsedToday measurement is greater than zero; the earlier start action was not observed.",
        )

    if previous_count == 0 and current_count == 0:
        return (
            STATUS_NOT_STARTED,
            "high",
            "A same-day zero decksUsedToday value was observed and no start transition is present.",
        )
    if previous_count == 0 and current_count == 1:
        return (
            STATUS_SOLO_START_OBSERVED,
            "high",
            "A same-day counter transition from 0 to 1 was observed; this is recorded as a solo start observation.",
        )
    if previous_count == 0 and current_count in {2, 3}:
        return (
            STATUS_DUEL_FIRST_LIKELY,
            "medium",
            f"A same-day counter transition from 0 to {current_count} was observed; a Duel-first start is likely from this counter pattern, not established as an action event.",
        )
    return (
        STATUS_UNKNOWN_START,
        "low",
        "No qualifying 0-to-1, 0-to-2, or 0-to-3 start transition was observed in the supplied same-day measurements; start type remains unknown.",
    )


def observe_duel_first(
    clan_tag: Any,
    season_id: Any,
    section_index: Any,
    race_created_at: Any,
    period_index: Any,
    player_tag: Any,
    current_decks_used_today: Any,
    previous_decks_used_today: Any = None,
    *,
    previous_race_day_key: Any = None,
    observed_at: Any = None,
    exempt: bool = False,
    api_stale: bool = False,
    existing_event_keys: Optional[Iterable[Any]] = None,
) -> DuelFirstResult:
    """Classify one player-day from two counter observations.

    ``previous_race_day_key`` is optional for callers that already scoped the
    previous snapshot to this day.  When supplied and different from the
    current key, the previous counter is ignored and the day is reset.  An
    explicit ``exempt`` flag takes precedence over ``api_stale`` because the
    caller has deliberately removed that player-day from classification; both
    controls suppress event creation.

    ``observed_at`` is required even for suppressed results so a later storage
    layer never has to invent an observation time.
    """

    if not isinstance(exempt, bool):
        raise _validation_error("exempt", "must be boolean")
    if not isinstance(api_stale, bool):
        raise _validation_error("api_stale", "must be boolean")

    race = build_race_identity(
        clan_tag,
        season_id,
        section_index,
        race_created_at,
    )
    identity = build_race_day_identity(race, period_index)
    normalized_player_tag = normalize_tag(player_tag, field="player_tag")
    if observed_at is None:
        raise _validation_error("observed_at", "is required")
    normalized_observed_at = normalize_timestamp(
        observed_at,
        field="observed_at",
    )

    current_count = normalize_counter(current_decks_used_today)
    previous_count = normalize_counter(
        previous_decks_used_today,
        field="previous_decks_used_today",
    )
    supplied_previous_key = _normalize_previous_day_key(previous_race_day_key)
    is_new_race_day = (
        supplied_previous_key is not None
        and supplied_previous_key != identity.race_day_key
    )
    has_same_day_previous = previous_count is not None and not is_new_race_day

    if has_same_day_previous and current_count is not None:
        if current_count < previous_count:
            raise _validation_error(
                "decks_used_today",
                "must not decrease within one race day",
            )

    if exempt:
        status = STATUS_EXEMPT
        confidence = "high"
        reason = "The caller explicitly marked this player-day as exempt; no start classification is made."
    elif api_stale:
        status = STATUS_API_STALE
        confidence = "unknown"
        reason = "The caller marked the API observation as stale; no start classification is made."
    else:
        status, confidence, reason = _classify(
            previous_count,
            current_count,
            has_same_day_previous=has_same_day_previous,
        )
        if is_new_race_day:
            reason = f"A new race_day_key was observed; the previous day counter was ignored. {reason}"

    event_type = status if status in _EVENT_STATUSES else None
    event_key = (
        _event_key(identity, normalized_player_tag, event_type)
        if event_type is not None
        else None
    )
    event_identity = (
        _event_identity(identity, normalized_player_tag, event_type)
        if event_type is not None
        else None
    )
    existing_keys = _normalize_existing_event_keys(existing_event_keys)
    event_exists = event_key is not None and event_key in existing_keys
    new_event = event_key is not None and not event_exists

    event: Optional[dict[str, Any]] = None
    if new_event:
        event = {
            # T06's storage mapper ignores this extra field, while T08 can use
            # it directly for idempotent orchestration or notification keys.
            "event_key": event_key,
            "clan_tag": identity.clan_tag,
            "race_created_at": identity.race_created_at,
            "period_index": identity.period_index,
            "player_tag": normalized_player_tag,
            "event_type": event_type,
            "observed_decks_used_today": current_count,
            "confidence": confidence,
            "observed_at": normalized_observed_at,
            "details": {
                "race_key": identity.race_key,
                "race_day_key": identity.race_day_key,
                "season_id": identity.season_id,
                "section_index": identity.section_index,
                "previous_decks_used_today": previous_count,
                "current_decks_used_today": current_count,
                "status": status,
                "reason": reason,
                "signal": "observed"
                if status == STATUS_SOLO_START_OBSERVED
                else "likely",
            },
        }

    return DuelFirstResult(
        status=status,
        confidence=confidence,
        reason=reason,
        observed_at=normalized_observed_at,
        player_tag=normalized_player_tag,
        identity=identity,
        previous_decks_used_today=previous_count,
        current_decks_used_today=current_count,
        is_new_race_day=is_new_race_day,
        event_key=event_key,
        event_identity=event_identity,
        event_exists=event_exists,
        new_event=new_event,
        event=event,
    )


# Descriptive aliases keep the pure domain entry point easy to discover while
# leaving one implementation and one state machine.
classify_duel_first = observe_duel_first
evaluate_duel_first = observe_duel_first


__all__ = [
    "DUEL_FIRST_STATUSES",
    "DuelFirstResult",
    "DuelFirstValidationError",
    "RaceDayIdentity",
    "RaceIdentity",
    "STATUS_API_STALE",
    "STATUS_DUEL_FIRST_LIKELY",
    "STATUS_EXEMPT",
    "STATUS_NOT_STARTED",
    "STATUS_SOLO_START_OBSERVED",
    "STATUS_UNKNOWN_START",
    "build_race_day_identity",
    "build_race_day_key",
    "build_race_identity",
    "build_race_key",
    "classify_duel_first",
    "evaluate_duel_first",
    "normalize_counter",
    "normalize_tag",
    "normalize_timestamp",
    "observe_duel_first",
]
