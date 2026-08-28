"""Stable, side-effect-free models for Clash Royale API payloads.

The official API has a few response shapes for the same concepts.  For
example, a river-race log is wrapped in ``items`` and ``standings[].clan``,
while the current-river-race endpoint exposes ``clan`` and ``clans``.  This
module is the boundary between those upstream shapes and the rest of the
application.

Only JSON values passed to the functions are inspected.  The normalizers do
not perform network requests, do not scrape HTML and do not retain an
upstream payload.  They accept either a raw JSON value or the
``ClashResponse`` envelope returned by :mod:`api.clash_client`.  Envelope
metadata is copied into a small safe metadata object so downstream routes can
make freshness and error state explicit.

Missing values remain ``None``.  Empty collections remain empty tuples, and
``field_status`` records whether a field was ``missing``, explicitly
``null``, ``empty``, ``invalid`` or present.  That distinction is useful for
analytics and prevents a missing performance metric from becoming a made-up
zero.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import json
import re
from types import MappingProxyType
from typing import Any, Dict, Iterable, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

try:  # Package import used by the application.
    from .clash_client import ClashResponse, normalize_tag as _client_normalize_tag
except ImportError:  # pragma: no cover - convenient when run as a loose file.
    from clash_client import ClashResponse, normalize_tag as _client_normalize_tag


UNKNOWN_SOURCE = "unknown"
DATA_STATUS_FRESH = "fresh"
DATA_STATUS_STALE = "stale"
DATA_STATUS_EMPTY = "empty"
DATA_STATUS_PARTIAL = "partial"
DATA_STATUS_INVALID = "invalid"
DATA_STATUS_ERROR = "error"
DATA_STATUS_UNKNOWN = "unknown"

PRESENCE_PRESENT = "present"
PRESENCE_MISSING = "missing"
PRESENCE_NULL = "null"
PRESENCE_EMPTY = "empty"
PRESENCE_INVALID = "invalid"
PRESENCE_FALLBACK = "fallback"
PRESENCE_METADATA = "metadata"
PRESENCE_CONTEXT = "context"
PRESENCE_DERIVED = "derived"

_MISSING = object()
_TIMESTAMP_FORMATS = (
    "%Y%m%dT%H%M%S.%fZ",
    "%Y%m%dT%H%M%SZ",
)
_DATA_STATUSES = frozenset(
    {
        DATA_STATUS_FRESH,
        DATA_STATUS_STALE,
        DATA_STATUS_EMPTY,
        DATA_STATUS_PARTIAL,
        DATA_STATUS_INVALID,
        DATA_STATUS_ERROR,
        DATA_STATUS_UNKNOWN,
    }
)


def _safe_timestamp(value: Any) -> Optional[str]:
    """Return a canonical UTC timestamp, or ``None`` for unusable input."""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        parsed = None
        for pattern in _TIMESTAMP_FORMATS:
            try:
                parsed = datetime.strptime(raw, pattern)
                parsed = parsed.replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        if parsed is None:
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except (TypeError, ValueError, OverflowError):
                return None
    else:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value
    return None


def _safe_endpoint(value: Any) -> Optional[str]:
    """Keep only a path from an endpoint, never query values or credentials."""

    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
        if parsed.scheme or parsed.netloc:
            return parsed.path or "/"
    except ValueError:
        pass
    return raw.split("?", 1)[0].split("#", 1)[0]


def _safe_icon_url(value: Any) -> Optional[str]:
    """Keep a usable absolute icon URL without credentials or query data."""

    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or any(character.isspace() or ord(character) < 32 for character in raw):
        return None
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        # Accessing .port also validates malformed ports without propagating
        # a ValueError from untrusted upstream JSON.
        _ = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or "@" in parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    # Query strings on CDN URLs can contain signed tokens or other sensitive
    # values.  There is no safe generic allow-list, so do not copy a URL that
    # would require carrying one downstream.  Fragments are not useful for
    # fetching an icon and are deliberately dropped.
    if parsed.query:
        return None
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc,
            parsed.path or "/",
            "",
            "",
        )
    )


def _safe_status(value: Any) -> str:
    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in _DATA_STATUSES:
            return candidate
    return DATA_STATUS_UNKNOWN


def _safe_tag(value: Any) -> Optional[str]:
    """Canonicalize plain, hash-prefixed and URL-encoded Clash tags."""

    try:
        # T01 owns the canonical tag contract used by request paths.  Calling
        # it here prevents the normalizer and client from ever disagreeing on
        # whether #TAG, %23TAG and TAG identify the same entity.
        return _client_normalize_tag(value)
    except Exception:
        return None


def _read(mapping: Any, *keys: str) -> Tuple[Any, str]:
    """Read the first known key while retaining its presence state."""

    if not isinstance(mapping, Mapping):
        return None, PRESENCE_INVALID
    for key in keys:
        if key not in mapping:
            continue
        value = mapping[key]
        if value is None:
            return None, PRESENCE_NULL
        if isinstance(value, (str, bytes, bytearray)) and not value.strip():
            return value, PRESENCE_EMPTY
        if isinstance(value, (list, tuple)) and not value:
            return value, PRESENCE_EMPTY
        return value, PRESENCE_PRESENT
    return None, PRESENCE_MISSING


def _read_with_fallback(
    mapping: Any,
    fallback: Any,
    *keys: str,
) -> Tuple[Any, str]:
    value, status = _read(mapping, *keys)
    if status == PRESENCE_MISSING and fallback is not _MISSING:
        return fallback, PRESENCE_FALLBACK
    return value, status


def _as_int(value: Any, status: str) -> Tuple[Optional[int], str]:
    if status in {PRESENCE_MISSING, PRESENCE_NULL, PRESENCE_EMPTY}:
        return None, status
    if isinstance(value, bool):
        return None, PRESENCE_INVALID
    if isinstance(value, int):
        return value, status
    # Numeric strings are accepted because stored history rows in this
    # repository can be JSON-encoded strings.  Floats are not truncated.
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        try:
            return int(value.strip()), status
        except (TypeError, ValueError, OverflowError):
            pass
    return None, PRESENCE_INVALID


def _as_text(value: Any, status: str) -> Tuple[Optional[str], str]:
    if status in {PRESENCE_MISSING, PRESENCE_NULL}:
        return None, status
    if isinstance(value, str):
        return value, status
    return None, PRESENCE_INVALID


def _as_tag(value: Any, status: str) -> Tuple[Optional[str], str]:
    if status in {PRESENCE_MISSING, PRESENCE_NULL, PRESENCE_EMPTY}:
        return None, status
    tag = _safe_tag(value)
    return (tag, status) if tag is not None else (None, PRESENCE_INVALID)


def _as_timestamp(value: Any, status: str) -> Tuple[Optional[str], str]:
    if status in {PRESENCE_MISSING, PRESENCE_NULL, PRESENCE_EMPTY}:
        return None, status
    timestamp = _safe_timestamp(value)
    return (timestamp, status) if timestamp is not None else (None, PRESENCE_INVALID)


def _metadata_dict(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    for method_name in ("as_dict", "to_dict"):
        method = getattr(value, method_name, None)
        if callable(method):
            try:
                result = method()
            except Exception:
                return {}
            return dict(result) if isinstance(result, Mapping) else {}
    return {}


@dataclass(frozen=True)
class NormalizationMetadata(Mapping[str, Any]):
    """Safe freshness and error metadata copied from a T01 response envelope."""

    source: str = UNKNOWN_SOURCE
    captured_at: Optional[str] = None
    fetched_at: Optional[str] = None
    data_status: str = DATA_STATUS_UNKNOWN
    is_stale: bool = False
    stale_reason: Optional[str] = None
    error_code: Optional[str] = None
    status_code: Optional[int] = None
    endpoint: Optional[str] = None

    @property
    def stale(self) -> bool:
        return self.is_stale

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "captured_at": self.captured_at,
            "fetched_at": self.fetched_at,
            "data_status": self.data_status,
            "is_stale": self.is_stale,
            "stale": self.is_stale,
            "stale_reason": self.stale_reason,
            "error_code": self.error_code,
            "status_code": self.status_code,
            "endpoint": self.endpoint,
        }

    to_dict = as_dict

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def __iter__(self):
        return iter(self.as_dict())

    def __len__(self) -> int:
        return len(self.as_dict())


def _metadata_from(
    raw_metadata: Any,
    payload: Any,
    *,
    metadata_override: Any = None,
    source: Optional[str] = None,
    captured_at: Any = None,
    fetched_at: Any = None,
    data_status: Optional[str] = None,
) -> NormalizationMetadata:
    raw = _metadata_dict(raw_metadata)
    if metadata_override is not None:
        raw.update(_metadata_dict(metadata_override))

    source_value = source if source is not None else raw.get("source", UNKNOWN_SOURCE)
    if not isinstance(source_value, str) or not source_value.strip():
        source_value = UNKNOWN_SOURCE
    else:
        source_value = source_value.strip()

    raw_captured = captured_at
    if raw_captured is None:
        raw_captured = raw.get("captured_at", raw.get("capturedAt"))
    raw_fetched = fetched_at
    if raw_fetched is None:
        raw_fetched = raw.get("fetched_at", raw.get("fetchedAt"))
    captured = _safe_timestamp(raw_captured)
    fetched = _safe_timestamp(raw_fetched)
    if captured is None:
        captured = fetched
    if fetched is None:
        fetched = captured

    stale_value = raw.get("is_stale", raw.get("stale", False))
    is_stale = stale_value if isinstance(stale_value, bool) else False
    status_value = data_status if data_status is not None else raw.get("data_status")
    status = _safe_status(status_value)
    if status == DATA_STATUS_UNKNOWN:
        if is_stale:
            status = DATA_STATUS_STALE
        elif payload is None or (
            isinstance(payload, (Mapping, list, tuple)) and not payload
        ):
            status = DATA_STATUS_EMPTY
        elif isinstance(payload, (Mapping, list, tuple)):
            status = DATA_STATUS_FRESH
        else:
            status = DATA_STATUS_INVALID
    if is_stale:
        status = DATA_STATUS_STALE

    error_code = raw.get("error_code")
    if not isinstance(error_code, str) or not error_code.strip():
        error_code = None
    stale_reason = raw.get("stale_reason")
    if not isinstance(stale_reason, str) or not stale_reason.strip():
        stale_reason = None

    status_code = raw.get("status_code")
    if isinstance(status_code, bool) or not isinstance(status_code, int):
        status_code = None
    endpoint = _safe_endpoint(raw.get("endpoint"))

    return NormalizationMetadata(
        source=source_value,
        captured_at=captured,
        fetched_at=fetched,
        data_status=status,
        is_stale=is_stale,
        stale_reason=stale_reason,
        error_code=error_code,
        status_code=status_code,
        endpoint=endpoint,
    )


@dataclass(frozen=True)
class _PayloadView:
    payload: Any
    metadata: NormalizationMetadata


def _looks_like_envelope(value: Any) -> bool:
    if isinstance(value, ClashResponse):
        return True
    if isinstance(value, Mapping):
        return "metadata" in value and ("data" in value or "payload" in value)
    return hasattr(value, "metadata") and hasattr(value, "data")


def _view(
    response: Any,
    *,
    metadata: Any = None,
    source: Optional[str] = None,
    captured_at: Any = None,
    fetched_at: Any = None,
    data_status: Optional[str] = None,
) -> _PayloadView:
    payload = response
    raw_metadata = None
    if isinstance(response, ClashResponse):
        payload = response.data
        raw_metadata = response.metadata
    elif isinstance(response, Mapping) and _looks_like_envelope(response):
        payload = response.get("data", response.get("payload"))
        raw_metadata = response.get("metadata")
    elif hasattr(response, "data") and hasattr(response, "metadata"):
        try:
            payload = response.data
            raw_metadata = response.metadata
        except Exception:
            payload = response
            raw_metadata = None
    return _PayloadView(
        payload=payload,
        metadata=_metadata_from(
            raw_metadata,
            payload,
            metadata_override=metadata,
            source=source,
            captured_at=captured_at,
            fetched_at=fetched_at,
            data_status=data_status,
        ),
    )


def _safe_json_value(value: Any) -> Any:
    """Recursively serialize only already-normalized, JSON-safe values."""

    if isinstance(value, _SerializableMapping):
        return value.as_dict()
    if isinstance(value, Mapping):
        return {str(key): _safe_json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_safe_json_value(item) for item in value]
    if isinstance(value, datetime):
        return _safe_timestamp(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return None


class _SerializableMapping(Mapping[str, Any]):
    """Small Mapping facade so normalized models work with route serializers."""

    def as_dict(self) -> Dict[str, Any]:  # pragma: no cover - implemented by models.
        raise NotImplementedError

    def to_dict(self) -> Dict[str, Any]:
        return self.as_dict()

    def to_json(self, **json_options: Any) -> str:
        return json.dumps(self.as_dict(), ensure_ascii=False, **json_options)

    def __getitem__(self, key: str) -> Any:
        return self.as_dict()[key]

    def __iter__(self):
        return iter(self.as_dict())

    def __len__(self) -> int:
        return len(self.as_dict())


def _freeze_statuses(statuses: Mapping[str, str]) -> Mapping[str, str]:
    return MappingProxyType(dict(statuses))


def _freeze_mapping(value: Optional[Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    if value is None:
        return None
    return MappingProxyType(deepcopy(dict(value)))


class _MetadataBacked:
    metadata: NormalizationMetadata

    @property
    def source(self) -> str:
        return self.metadata.source

    @property
    def captured_at(self) -> Optional[str]:
        return self.metadata.captured_at

    @property
    def fetched_at(self) -> Optional[str]:
        return self.metadata.fetched_at

    @property
    def data_status(self) -> str:
        return self.metadata.data_status

    @property
    def is_stale(self) -> bool:
        return self.metadata.is_stale

    @property
    def stale(self) -> bool:
        return self.metadata.is_stale

    @property
    def stale_reason(self) -> Optional[str]:
        return self.metadata.stale_reason

    @property
    def error_code(self) -> Optional[str]:
        return self.metadata.error_code


@dataclass(frozen=True)
class CardProfile(_SerializableMapping):
    """Allow-listed immutable card fields from a player profile."""

    name: Optional[str] = None
    card_id: Optional[int] = None
    level: Optional[int] = None
    max_level: Optional[int] = None
    count: Optional[int] = None
    star_level: Optional[int] = None
    evolution_level: Optional[int] = None
    icon_urls: Optional[Mapping[str, str]] = None
    field_status: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "field_status", _freeze_statuses(self.field_status))
        if self.icon_urls is not None:
            object.__setattr__(
                self,
                "icon_urls",
                MappingProxyType(dict(self.icon_urls)),
            )

    @property
    def id(self) -> Optional[int]:
        return self.card_id

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "card_id": self.card_id,
            "level": self.level,
            "max_level": self.max_level,
            "count": self.count,
            "star_level": self.star_level,
            "evolution_level": self.evolution_level,
            "icon_urls": _safe_json_value(self.icon_urls),
            "field_status": dict(self.field_status),
        }


@dataclass(frozen=True)
class RaceContext(_MetadataBacked, _SerializableMapping):
    """Stable context for a current or historic river-race snapshot."""

    clan_tag: Optional[str] = None
    season_id: Optional[int] = None
    section_index: Optional[int] = None
    period_index: Optional[int] = None
    period_type: Optional[str] = None
    state: Optional[str] = None
    race_created_at: Optional[str] = None
    metadata: NormalizationMetadata = field(default_factory=NormalizationMetadata)
    field_status: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "field_status", _freeze_statuses(self.field_status))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "clan_tag": self.clan_tag,
            "season_id": self.season_id,
            "section_index": self.section_index,
            "period_index": self.period_index,
            "period_type": self.period_type,
            "state": self.state,
            "race_created_at": self.race_created_at,
            "captured_at": self.captured_at,
            "source": self.source,
            "data_status": self.data_status,
            "is_stale": self.is_stale,
            "stale_reason": self.stale_reason,
            "error_code": self.error_code,
            "metadata": self.metadata.as_dict(),
            "field_status": dict(self.field_status),
        }


@dataclass(frozen=True)
class RaceParticipant(_MetadataBacked, _SerializableMapping):
    """Stable participant metrics; omitted upstream metrics remain ``None``."""

    player_tag: Optional[str] = None
    name: Optional[str] = None
    role: Optional[str] = None
    fame: Optional[int] = None
    repair_points: Optional[int] = None
    decks_used: Optional[int] = None
    decks_used_today: Optional[int] = None
    boat_attacks: Optional[int] = None
    boat_attacks_today: Optional[int] = None
    boat_defenses: Optional[int] = None
    boat_defenses_today: Optional[int] = None
    metadata: NormalizationMetadata = field(default_factory=NormalizationMetadata)
    field_status: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "field_status", _freeze_statuses(self.field_status))

    @property
    def tag(self) -> Optional[str]:
        return self.player_tag

    def as_dict(self) -> Dict[str, Any]:
        return {
            "player_tag": self.player_tag,
            "name": self.name,
            "role": self.role,
            "fame": self.fame,
            "repair_points": self.repair_points,
            "decks_used": self.decks_used,
            "decks_used_today": self.decks_used_today,
            "boat_attacks": self.boat_attacks,
            "boat_attacks_today": self.boat_attacks_today,
            "boat_defenses": self.boat_defenses,
            "boat_defenses_today": self.boat_defenses_today,
            "captured_at": self.captured_at,
            "source": self.source,
            "data_status": self.data_status,
            "is_stale": self.is_stale,
            "stale_reason": self.stale_reason,
            "error_code": self.error_code,
            "metadata": self.metadata.as_dict(),
            "field_status": dict(self.field_status),
        }


@dataclass(frozen=True)
class RaceClan(_MetadataBacked, _SerializableMapping):
    """Stable clan identity and race totals for one race snapshot."""

    clan_tag: Optional[str] = None
    name: Optional[str] = None
    member_count: Optional[int] = None
    clan_type: Optional[str] = None
    badge_id: Optional[int] = None
    clan_score: Optional[int] = None
    clan_war_trophies: Optional[int] = None
    rank: Optional[int] = None
    fame: Optional[int] = None
    repair_points: Optional[int] = None
    decks_used: Optional[int] = None
    decks_used_today: Optional[int] = None
    boat_attacks: Optional[int] = None
    boat_attacks_today: Optional[int] = None
    boat_defenses: Optional[int] = None
    boat_defenses_today: Optional[int] = None
    battles_played: Optional[int] = None
    wins: Optional[int] = None
    collection_day_battles_played: Optional[int] = None
    battles_remaining: Optional[int] = None
    is_opponent: Optional[bool] = None
    participants: Optional[Tuple[RaceParticipant, ...]] = None
    metadata: NormalizationMetadata = field(default_factory=NormalizationMetadata)
    field_status: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "field_status", _freeze_statuses(self.field_status))

    @property
    def tag(self) -> Optional[str]:
        return self.clan_tag

    def as_dict(self) -> Dict[str, Any]:
        return {
            "clan_tag": self.clan_tag,
            "name": self.name,
            "member_count": self.member_count,
            "clan_type": self.clan_type,
            "badge_id": self.badge_id,
            "clan_score": self.clan_score,
            "clan_war_trophies": self.clan_war_trophies,
            "rank": self.rank,
            "fame": self.fame,
            "repair_points": self.repair_points,
            "decks_used": self.decks_used,
            "decks_used_today": self.decks_used_today,
            "boat_attacks": self.boat_attacks,
            "boat_attacks_today": self.boat_attacks_today,
            "boat_defenses": self.boat_defenses,
            "boat_defenses_today": self.boat_defenses_today,
            "battles_played": self.battles_played,
            "wins": self.wins,
            "collection_day_battles_played": self.collection_day_battles_played,
            "battles_remaining": self.battles_remaining,
            "is_opponent": self.is_opponent,
            "participants": _safe_json_value(self.participants),
            "captured_at": self.captured_at,
            "source": self.source,
            "data_status": self.data_status,
            "is_stale": self.is_stale,
            "stale_reason": self.stale_reason,
            "error_code": self.error_code,
            "metadata": self.metadata.as_dict(),
            "field_status": dict(self.field_status),
        }


_SAFE_RECORD_KEYS = frozenset(
    {
        "name",
        "id",
        "level",
        "maxLevel",
        "count",
        "starLevel",
        "evolutionLevel",
        "progress",
        "target",
        "value",
        "amount",
        "type",
        "tag",
        "rank",
        "trophies",
        "bestTrophies",
        "wins",
        "losses",
        "seasonId",
        "clanRank",
        "previousClanRank",
        "completed",
        "iconUrls",
        "currentSeason",
        "previousSeason",
        "bestSeason",
    }
)


def _safe_record(value: Any) -> Mapping[str, Any]:
    """Copy a known profile record without retaining arbitrary upstream keys."""

    if not isinstance(value, Mapping):
        return MappingProxyType({})
    result: Dict[str, Any] = {}
    for key in _SAFE_RECORD_KEYS:
        if key not in value:
            continue
        item = value[key]
        if isinstance(item, Mapping):
            result[key] = dict(_safe_record(item))
        elif isinstance(item, (str, int, float, bool)) or item is None:
            result[key] = item
        elif isinstance(item, (list, tuple)):
            result[key] = [
                _safe_json_value(entry)
                for entry in item
                if isinstance(entry, (str, int, float, bool)) or entry is None
            ]
    return MappingProxyType(result)


@dataclass(frozen=True)
class PlayerProfile(_MetadataBacked, _SerializableMapping):
    """Stable player identity, profile statistics and card fields."""

    player_tag: Optional[str] = None
    name: Optional[str] = None
    role: Optional[str] = None
    trophies: Optional[int] = None
    best_trophies: Optional[int] = None
    exp_level: Optional[int] = None
    wins: Optional[int] = None
    losses: Optional[int] = None
    battle_count: Optional[int] = None
    three_crown_wins: Optional[int] = None
    challenge_cards_won: Optional[int] = None
    tournament_cards_won: Optional[int] = None
    clan_tag: Optional[str] = None
    clan_name: Optional[str] = None
    clan_score: Optional[int] = None
    clan_war_trophies: Optional[int] = None
    clan_badge_id: Optional[int] = None
    arena_id: Optional[int] = None
    arena_name: Optional[str] = None
    current_favourite_card: Optional[CardProfile] = None
    cards: Optional[Tuple[CardProfile, ...]] = None
    achievements: Optional[Tuple[Mapping[str, Any], ...]] = None
    badges: Optional[Tuple[Mapping[str, Any], ...]] = None
    league_statistics: Optional[Mapping[str, Any]] = None
    metadata: NormalizationMetadata = field(default_factory=NormalizationMetadata)
    field_status: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "field_status", _freeze_statuses(self.field_status))
        object.__setattr__(self, "league_statistics", _freeze_mapping(self.league_statistics))

    @property
    def tag(self) -> Optional[str]:
        return self.player_tag

    @property
    def bestTrophies(self) -> Optional[int]:  # Compatibility for callers migrating from API names.
        return self.best_trophies

    def as_dict(self) -> Dict[str, Any]:
        return {
            "player_tag": self.player_tag,
            "name": self.name,
            "role": self.role,
            "trophies": self.trophies,
            "best_trophies": self.best_trophies,
            "exp_level": self.exp_level,
            "wins": self.wins,
            "losses": self.losses,
            "battle_count": self.battle_count,
            "three_crown_wins": self.three_crown_wins,
            "challenge_cards_won": self.challenge_cards_won,
            "tournament_cards_won": self.tournament_cards_won,
            "clan_tag": self.clan_tag,
            "clan_name": self.clan_name,
            "clan_score": self.clan_score,
            "clan_war_trophies": self.clan_war_trophies,
            "clan_badge_id": self.clan_badge_id,
            "arena_id": self.arena_id,
            "arena_name": self.arena_name,
            "current_favourite_card": _safe_json_value(self.current_favourite_card),
            "cards": _safe_json_value(self.cards),
            "achievements": _safe_json_value(self.achievements),
            "badges": _safe_json_value(self.badges),
            "league_statistics": _safe_json_value(self.league_statistics),
            "captured_at": self.captured_at,
            "source": self.source,
            "data_status": self.data_status,
            "is_stale": self.is_stale,
            "stale_reason": self.stale_reason,
            "error_code": self.error_code,
            "metadata": self.metadata.as_dict(),
            "field_status": dict(self.field_status),
        }


@dataclass(frozen=True)
class NormalizedRiverRace(_MetadataBacked, _SerializableMapping):
    """Combined normalized current-race view convenient for future routes."""

    context: RaceContext
    clans: Tuple[RaceClan, ...] = ()
    participants: Tuple[RaceParticipant, ...] = ()
    metadata: NormalizationMetadata = field(default_factory=NormalizationMetadata)
    field_status: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "field_status", _freeze_statuses(self.field_status))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "context": self.context.as_dict(),
            "clans": _safe_json_value(self.clans),
            "participants": _safe_json_value(self.participants),
            "captured_at": self.captured_at,
            "source": self.source,
            "data_status": self.data_status,
            "is_stale": self.is_stale,
            "stale_reason": self.metadata.stale_reason,
            "error_code": self.metadata.error_code,
            "metadata": self.metadata.as_dict(),
            "field_status": dict(self.field_status),
        }


def _status_for(
    metadata: NormalizationMetadata,
    statuses: Mapping[str, str],
    *,
    required: Iterable[str] = (),
    empty: bool = False,
) -> str:
    if metadata.is_stale or metadata.data_status == DATA_STATUS_STALE:
        return DATA_STATUS_STALE
    if metadata.data_status == DATA_STATUS_ERROR:
        return DATA_STATUS_ERROR
    if metadata.data_status == DATA_STATUS_INVALID:
        return DATA_STATUS_INVALID
    if empty or metadata.data_status == DATA_STATUS_EMPTY:
        return DATA_STATUS_EMPTY
    if any(status == PRESENCE_INVALID for status in statuses.values()):
        return DATA_STATUS_INVALID
    if any(statuses.get(key) == PRESENCE_INVALID for key in required):
        return DATA_STATUS_INVALID
    if any(
        statuses.get(key)
        in {
            PRESENCE_MISSING,
            PRESENCE_NULL,
            PRESENCE_EMPTY,
        }
        for key in required
    ):
        return DATA_STATUS_PARTIAL
    if metadata.data_status == DATA_STATUS_PARTIAL:
        return DATA_STATUS_PARTIAL
    return DATA_STATUS_FRESH


_RACE_KEYS = frozenset(
    {
        "seasonId",
        "season_id",
        "createdDate",
        "createdAt",
        "created_at",
        "raceCreatedAt",
        "race_created_at",
        "sectionIndex",
        "periodIndex",
        "periodType",
        "state",
        "raceState",
        "race_state",
        "standings",
        "clans",
        "clan",
    }
)


def _race_root(payload: Any) -> Tuple[Mapping[str, Any], str]:
    """Select a race item from either a direct payload or an ``items`` list."""

    if isinstance(payload, Mapping):
        items, items_status = _read(payload, "items")
        if items_status != PRESENCE_MISSING:
            if items_status in {PRESENCE_NULL, PRESENCE_EMPTY}:
                return {}, items_status
            if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
                return {}, PRESENCE_INVALID
            for item in items:
                if isinstance(item, Mapping) and _RACE_KEYS.intersection(item):
                    return item, PRESENCE_PRESENT
            return {}, PRESENCE_INVALID if items else PRESENCE_EMPTY
        return payload, PRESENCE_PRESENT if payload else PRESENCE_EMPTY
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        if not payload:
            return {}, PRESENCE_EMPTY
        for item in payload:
            if isinstance(item, Mapping):
                return item, PRESENCE_PRESENT
        return {}, PRESENCE_INVALID
    return {}, PRESENCE_INVALID if payload is not None else PRESENCE_EMPTY


def _clan_entries(root: Mapping[str, Any]) -> list[Tuple[Mapping[str, Any], Optional[Mapping[str, Any]]]]:
    entries: list[Tuple[Mapping[str, Any], Optional[Mapping[str, Any]]]] = []

    direct, direct_status = _read(root, "clan")
    if isinstance(direct, Mapping):
        entries.append((direct, None))

    clans, clans_status = _read(root, "clans")
    if clans_status not in {PRESENCE_MISSING, PRESENCE_NULL, PRESENCE_EMPTY}:
        if isinstance(clans, Sequence) and not isinstance(clans, (str, bytes, bytearray)):
            entries.extend((clan, None) for clan in clans if isinstance(clan, Mapping))

    standings, standings_status = _read(root, "standings")
    if standings_status not in {PRESENCE_MISSING, PRESENCE_NULL, PRESENCE_EMPTY}:
        if isinstance(standings, Sequence) and not isinstance(standings, (str, bytes, bytearray)):
            for standing in standings:
                if not isinstance(standing, Mapping):
                    continue
                clan, clan_status = _read(standing, "clan")
                if isinstance(clan, Mapping):
                    entries.append((clan, standing))
                elif _looks_like_clan(standing):
                    entries.append((standing, standing))

    if not entries and _looks_like_clan(root):
        entries.append((root, None))
    return entries


def _looks_like_clan(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    tag, tag_status = _read(value, "tag", "clanTag", "clan_tag")
    return tag_status not in {PRESENCE_MISSING, PRESENCE_NULL} and _safe_tag(tag) is not None


def _select_clan_entry(
    entries: Iterable[Tuple[Mapping[str, Any], Optional[Mapping[str, Any]]]],
    clan_tag: Optional[str],
) -> Optional[Tuple[Mapping[str, Any], Optional[Mapping[str, Any]]]]:
    entries_list = list(entries)
    wanted = _safe_tag(clan_tag)
    if wanted is not None:
        for entry in entries_list:
            tag, _ = _read(entry[0], "tag", "clanTag", "clan_tag")
            if _safe_tag(tag) == wanted:
                return entry
        return None
    return entries_list[0] if entries_list else None


def _participant_values(
    node: Mapping[str, Any],
    standing: Optional[Mapping[str, Any]] = None,
) -> Tuple[Any, str]:
    participants, status = _read(node, "participants")
    if status == PRESENCE_MISSING and standing is not None:
        return _read(standing, "participants")
    return participants, status


def _profile_card(value: Any) -> Optional[CardProfile]:
    if not isinstance(value, Mapping):
        return None

    statuses: Dict[str, str] = {}

    raw, status = _read(value, "name")
    name, statuses["name"] = _as_text(raw, status)
    raw, status = _read(value, "id", "cardId", "card_id")
    card_id, statuses["card_id"] = _as_int(raw, status)
    numbers: Dict[str, Optional[int]] = {}
    for output, *keys in (
        ("level", "level"),
        ("max_level", "maxLevel", "max_level"),
        ("count", "count"),
        ("star_level", "starLevel", "star_level"),
        ("evolution_level", "evolutionLevel", "evolution_level"),
    ):
        raw, status = _read(value, *keys)
        numbers[output], statuses[output] = _as_int(raw, status)

    raw, status = _read(value, "iconUrls", "icon_urls")
    icon_urls: Optional[Mapping[str, str]] = None
    if status in {PRESENCE_MISSING, PRESENCE_NULL, PRESENCE_EMPTY}:
        statuses["icon_urls"] = status
    elif isinstance(raw, Mapping):
        safe_urls = {
            key: _safe_icon_url(item)
            for key, item in raw.items()
            if key in {"small", "medium", "evolutionMedium"} and isinstance(item, str)
        }
        safe_urls = {key: item for key, item in safe_urls.items() if item is not None}
        icon_urls = safe_urls
        statuses["icon_urls"] = status
    else:
        statuses["icon_urls"] = PRESENCE_INVALID

    return CardProfile(
        name=name,
        card_id=card_id,
        level=numbers["level"],
        max_level=numbers["max_level"],
        count=numbers["count"],
        star_level=numbers["star_level"],
        evolution_level=numbers["evolution_level"],
        icon_urls=icon_urls,
        field_status=statuses,
    )


def _profile_records(value: Any, status: str) -> Tuple[Optional[Tuple[Mapping[str, Any], ...]], str]:
    if status in {PRESENCE_MISSING, PRESENCE_NULL, PRESENCE_EMPTY}:
        return (None if status != PRESENCE_EMPTY else ()), status
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return None, PRESENCE_INVALID
    records = tuple(_safe_record(item) for item in value if isinstance(item, Mapping))
    return records, status


def _profile_root(payload: Any) -> Mapping[str, Any]:
    if isinstance(payload, Mapping):
        items, status = _read(payload, "items")
        if status in {PRESENCE_NULL, PRESENCE_EMPTY}:
            return {}
        if status == PRESENCE_PRESENT:
            if isinstance(items, Sequence) and not isinstance(items, (str, bytes, bytearray)):
                for item in items:
                    if isinstance(item, Mapping):
                        return item
            return {}
        return payload
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        for item in payload:
            if isinstance(item, Mapping):
                return item
    return {}


def _normalize_player_mapping(
    root: Mapping[str, Any],
    metadata: NormalizationMetadata,
    *,
    fallback_player_tag: Optional[str] = None,
    fallback_clan_tag: Optional[str] = None,
) -> PlayerProfile:
    statuses: Dict[str, str] = {}

    raw, status = _read_with_fallback(root, fallback_player_tag, "tag", "playerTag", "player_tag")
    player_tag, statuses["player_tag"] = _as_tag(raw, status)
    raw, status = _read(root, "name")
    name, statuses["name"] = _as_text(raw, status)
    raw, status = _read(root, "role")
    role, statuses["role"] = _as_text(raw, status)

    numeric_fields = (
        ("trophies", "trophies"),
        ("best_trophies", "bestTrophies", "best_trophies"),
        ("exp_level", "expLevel", "exp_level"),
        ("wins", "wins"),
        ("losses", "losses"),
        ("battle_count", "battleCount", "battle_count"),
        ("three_crown_wins", "threeCrownWins", "three_crown_wins"),
        ("challenge_cards_won", "challengeCardsWon", "challenge_cards_won"),
        ("tournament_cards_won", "tournamentCardsWon", "tournament_cards_won"),
    )
    numbers: Dict[str, Optional[int]] = {}
    for output, *keys in numeric_fields:
        raw, status = _read(root, *keys)
        numbers[output], statuses[output] = _as_int(raw, status)

    clan_raw, clan_status = _read(root, "clan")
    clan = clan_raw if isinstance(clan_raw, Mapping) else None
    if clan_status in {PRESENCE_NULL, PRESENCE_EMPTY}:
        clan = None
    if clan_status == PRESENCE_MISSING and fallback_clan_tag is not None:
        clan = {"tag": fallback_clan_tag}
        clan_status = PRESENCE_CONTEXT

    if clan is not None:
        raw, status = _read(clan, "tag", "clanTag", "clan_tag")
        clan_tag, statuses["clan_tag"] = _as_tag(raw, status)
        raw, status = _read(clan, "name")
        clan_name, statuses["clan_name"] = _as_text(raw, status)
        for output, *keys in (
            ("clan_score", "clanScore", "clan_score"),
            ("clan_war_trophies", "clanWarTrophies", "clan_war_trophies"),
            ("clan_badge_id", "badgeId", "clanBadgeId", "clan_badge_id"),
        ):
            raw, status = _read(clan, *keys)
            numbers[output], statuses[output] = _as_int(raw, status)
    else:
        clan_tag = clan_name = None
        for key in ("clan_tag", "clan_name", "clan_badge_id", "clan_score", "clan_war_trophies"):
            statuses[key] = clan_status

    raw, status = _read(root, "arena")
    if isinstance(raw, Mapping):
        arena_id_raw, arena_id_status = _read(raw, "id", "arenaId", "arena_id")
        arena_id, statuses["arena_id"] = _as_int(arena_id_raw, arena_id_status)
        arena_name_raw, arena_name_status = _read(raw, "name")
        arena_name, statuses["arena_name"] = _as_text(arena_name_raw, arena_name_status)
        statuses["arena"] = status
    elif status in {PRESENCE_MISSING, PRESENCE_NULL, PRESENCE_EMPTY}:
        arena_id = arena_name = None
        statuses["arena_id"] = statuses["arena_name"] = status
        statuses["arena"] = status
    else:
        arena_id = arena_name = None
        statuses["arena_id"] = statuses["arena_name"] = PRESENCE_INVALID
        statuses["arena"] = PRESENCE_INVALID

    raw, status = _read(root, "currentFavouriteCard", "current_favourite_card")
    if status in {PRESENCE_MISSING, PRESENCE_NULL, PRESENCE_EMPTY}:
        current_card = None
        statuses["current_favourite_card"] = status
    else:
        current_card = _profile_card(raw)
        statuses["current_favourite_card"] = status if current_card is not None else PRESENCE_INVALID

    raw, status = _read(root, "cards")
    if status in {PRESENCE_MISSING, PRESENCE_NULL, PRESENCE_EMPTY}:
        cards = None if status != PRESENCE_EMPTY else ()
        statuses["cards"] = status
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        cards = tuple(card for item in raw if (card := _profile_card(item)) is not None)
        statuses["cards"] = status
    else:
        cards = None
        statuses["cards"] = PRESENCE_INVALID

    for output, *keys in (
        ("achievements", "achievements"),
        ("badges", "badges"),
    ):
        raw, status = _read(root, *keys)
        records, record_status = _profile_records(raw, status)
        if output == "achievements":
            achievements = records
        else:
            badges = records
        statuses[output] = record_status

    raw, status = _read(root, "leagueStatistics", "league_statistics")
    if status in {PRESENCE_MISSING, PRESENCE_NULL, PRESENCE_EMPTY}:
        league_statistics = None
        statuses["league_statistics"] = status
    elif isinstance(raw, Mapping):
        league_statistics = _safe_record(raw)
        statuses["league_statistics"] = status
    else:
        league_statistics = None
        statuses["league_statistics"] = PRESENCE_INVALID

    required = ("player_tag", "name")
    status_value = _status_for(metadata, statuses, required=required, empty=not root)
    object_metadata = NormalizationMetadata(
        source=metadata.source,
        captured_at=metadata.captured_at,
        fetched_at=metadata.fetched_at,
        data_status=status_value,
        is_stale=metadata.is_stale,
        stale_reason=metadata.stale_reason,
        error_code=metadata.error_code,
        status_code=metadata.status_code,
        endpoint=metadata.endpoint,
    )
    return PlayerProfile(
        player_tag=player_tag,
        name=name,
        role=role,
        trophies=numbers["trophies"],
        best_trophies=numbers["best_trophies"],
        exp_level=numbers["exp_level"],
        wins=numbers["wins"],
        losses=numbers["losses"],
        battle_count=numbers["battle_count"],
        three_crown_wins=numbers["three_crown_wins"],
        challenge_cards_won=numbers["challenge_cards_won"],
        tournament_cards_won=numbers["tournament_cards_won"],
        clan_tag=clan_tag,
        clan_name=clan_name,
        clan_score=numbers.get("clan_score"),
        clan_war_trophies=numbers.get("clan_war_trophies"),
        clan_badge_id=numbers.get("clan_badge_id"),
        arena_id=arena_id,
        arena_name=arena_name,
        current_favourite_card=current_card,
        cards=cards,
        achievements=achievements,
        badges=badges,
        league_statistics=league_statistics,
        metadata=object_metadata,
        field_status=statuses,
    )


def normalize_player_profile(
    response: Any,
    *,
    player_tag: Optional[str] = None,
    clan_tag: Optional[str] = None,
    metadata: Any = None,
    source: Optional[str] = None,
    captured_at: Any = None,
    fetched_at: Any = None,
    data_status: Optional[str] = None,
) -> PlayerProfile:
    """Normalize an official ``/players/{tag}`` response or one member item."""

    view = _view(
        response,
        metadata=metadata,
        source=source,
        captured_at=captured_at,
        fetched_at=fetched_at,
        data_status=data_status,
    )
    root = _profile_root(view.payload)
    return _normalize_player_mapping(
        root,
        view.metadata,
        fallback_player_tag=player_tag,
        fallback_clan_tag=clan_tag,
    )


def normalize_members(
    response: Any,
    *,
    clan_tag: Optional[str] = None,
    metadata: Any = None,
    source: Optional[str] = None,
    captured_at: Any = None,
    fetched_at: Any = None,
    data_status: Optional[str] = None,
) -> Tuple[PlayerProfile, ...]:
    """Normalize a members response, deduplicating by canonical player tag."""

    view = _view(
        response,
        metadata=metadata,
        source=source,
        captured_at=captured_at,
        fetched_at=fetched_at,
        data_status=data_status,
    )
    payload = view.payload
    if isinstance(payload, Mapping):
        items, status = _read(payload, "items", "members", "memberList")
    elif isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        items, status = payload, PRESENCE_PRESENT if payload else PRESENCE_EMPTY
    else:
        return ()
    if status in {PRESENCE_MISSING, PRESENCE_NULL, PRESENCE_EMPTY}:
        return ()
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
        return ()

    deduped: Dict[str, PlayerProfile] = {}
    for item in items:
        if isinstance(item, PlayerProfile):
            profile = item
        elif isinstance(item, Mapping):
            profile = _normalize_player_mapping(
                item,
                view.metadata,
                fallback_clan_tag=clan_tag,
            )
        else:
            continue
        if profile.player_tag is None:
            continue
        # Last upstream row wins while the canonical tag remains the key.  A
        # name change therefore updates one identity instead of creating two.
        deduped[profile.player_tag] = profile
    return tuple(deduped.values())


def _participant_lookup(members: Any, metadata: NormalizationMetadata) -> Dict[str, PlayerProfile]:
    if members is None:
        return {}
    if isinstance(members, Mapping) and not isinstance(members, PlayerProfile):
        if _looks_like_envelope(members) or "items" in members or "members" in members or "memberList" in members:
            profiles = normalize_members(members, metadata=metadata)
        else:
            profile_rows = []
            for key, raw in members.items():
                if isinstance(raw, PlayerProfile):
                    profile_rows.append(raw)
                elif isinstance(raw, Mapping):
                    profile_rows.append(
                        _normalize_player_mapping(
                            raw,
                            metadata,
                            fallback_player_tag=key,
                        )
                    )
            profiles = tuple(profile_rows)
    elif isinstance(members, Sequence) and not isinstance(members, (str, bytes, bytearray)):
        profiles = tuple(
            item
            if isinstance(item, PlayerProfile)
            else _normalize_player_mapping(item, metadata)
            for item in members
            if isinstance(item, (PlayerProfile, Mapping))
        )
    else:
        profiles = ()
    return {
        profile.player_tag: profile
        for profile in profiles
        if profile.player_tag is not None
    }


def _normalize_participant_mapping(
    raw: Mapping[str, Any],
    metadata: NormalizationMetadata,
    *,
    member: Optional[PlayerProfile] = None,
) -> Optional[RaceParticipant]:
    statuses: Dict[str, str] = {}
    member_tag = member.player_tag if member is not None else _MISSING
    value, status = _read_with_fallback(raw, member_tag, "tag", "playerTag", "player_tag")
    player_tag, statuses["player_tag"] = _as_tag(value, status)
    if player_tag is None:
        # An unidentifiable row cannot safely be joined to a player identity.
        return None

    member_name = member.name if member is not None else _MISSING
    value, status = _read_with_fallback(raw, member_name, "name")
    name, statuses["name"] = _as_text(value, status)
    member_role = member.role if member is not None else _MISSING
    value, status = _read_with_fallback(raw, member_role, "role")
    role, statuses["role"] = _as_text(value, status)

    numbers: Dict[str, Optional[int]] = {}
    for output, *keys in (
        ("fame", "fame"),
        ("repair_points", "repairPoints", "repair_points"),
        ("decks_used", "decksUsed", "decks_used", "decks_total_so_far"),
        ("decks_used_today", "decksUsedToday", "decks_used_today"),
        ("boat_attacks", "boatAttacks", "boat_attacks"),
        ("boat_attacks_today", "boatAttacksToday", "boat_attacks_today"),
        ("boat_defenses", "boatDefenses", "boat_defenses"),
        ("boat_defenses_today", "boatDefensesToday", "boat_defenses_today"),
    ):
        value, status = _read(raw, *keys)
        numbers[output], statuses[output] = _as_int(value, status)

    required = (
        "player_tag",
        "name",
        "role",
        "fame",
        "repair_points",
        "decks_used",
        "decks_used_today",
        "boat_attacks",
        "boat_attacks_today",
        "boat_defenses",
        "boat_defenses_today",
    )
    status_value = _status_for(metadata, statuses, required=required)
    object_metadata = NormalizationMetadata(
        source=metadata.source,
        captured_at=metadata.captured_at,
        fetched_at=metadata.fetched_at,
        data_status=status_value,
        is_stale=metadata.is_stale,
        stale_reason=metadata.stale_reason,
        error_code=metadata.error_code,
        status_code=metadata.status_code,
        endpoint=metadata.endpoint,
    )
    return RaceParticipant(
        player_tag=player_tag,
        name=name,
        role=role,
        fame=numbers["fame"],
        repair_points=numbers["repair_points"],
        decks_used=numbers["decks_used"],
        decks_used_today=numbers["decks_used_today"],
        boat_attacks=numbers["boat_attacks"],
        boat_attacks_today=numbers["boat_attacks_today"],
        boat_defenses=numbers["boat_defenses"],
        boat_defenses_today=numbers["boat_defenses_today"],
        metadata=object_metadata,
        field_status=statuses,
    )


def _normalize_race_clan_mapping(
    node: Mapping[str, Any],
    standing: Optional[Mapping[str, Any]],
    metadata: NormalizationMetadata,
    *,
    context_clan_tag: Optional[str] = None,
    members_lookup: Optional[Mapping[str, PlayerProfile]] = None,
) -> Optional[RaceClan]:
    statuses: Dict[str, str] = {}
    value, status = _read(node, "tag", "clanTag", "clan_tag")
    clan_tag, statuses["clan_tag"] = _as_tag(value, status)
    if clan_tag is None:
        return None
    value, status = _read(node, "name")
    name, statuses["name"] = _as_text(value, status)

    numbers: Dict[str, Optional[int]] = {}
    value, status = _read(node, "members", "memberCount", "member_count")
    numbers["member_count"], statuses["member_count"] = _as_int(value, status)
    value, status = _read(node, "badgeId", "badge_id")
    numbers["badge_id"], statuses["badge_id"] = _as_int(value, status)
    value, status = _read(node, "type", "clanType", "clan_type")
    clan_type, statuses["clan_type"] = _as_text(value, status)
    for output, *keys in (
        ("clan_score", "clanScore", "clan_score"),
        ("clan_war_trophies", "clanWarTrophies", "clan_war_trophies"),
        ("rank", "rank", "clanRank", "clan_rank"),
        ("fame", "fame"),
        ("repair_points", "repairPoints", "repair_points"),
        ("decks_used", "decksUsed", "decks_used"),
        ("decks_used_today", "decksUsedToday", "decks_used_today"),
        ("boat_attacks", "boatAttacks", "boat_attacks"),
        ("boat_attacks_today", "boatAttacksToday", "boat_attacks_today"),
        ("boat_defenses", "boatDefenses", "boat_defenses"),
        ("boat_defenses_today", "boatDefensesToday", "boat_defenses_today"),
        ("battles_played", "battlesPlayed", "battles_played"),
        ("wins", "wins"),
        (
            "collection_day_battles_played",
            "collectionDayBattlesPlayed",
            "collection_day_battles_played",
        ),
        ("battles_remaining", "battlesRemaining", "battles_remaining"),
    ):
        value, status = _read(node, *keys)
        if output == "rank" and status == PRESENCE_MISSING and standing is not None:
            value, status = _read(standing, "rank", "clanRank", "clan_rank")
        numbers[output], statuses[output] = _as_int(value, status)

    participants_raw, participants_status = _participant_values(node, standing)
    statuses["participants"] = participants_status
    participant_rows: Optional[Tuple[RaceParticipant, ...]]
    if participants_status in {PRESENCE_MISSING, PRESENCE_NULL}:
        participant_rows = None
    elif participants_status == PRESENCE_EMPTY:
        participant_rows = ()
    elif isinstance(participants_raw, Sequence) and not isinstance(
        participants_raw,
        (str, bytes, bytearray),
    ):
        normalized: list[RaceParticipant] = []
        lookup = members_lookup or {}
        for raw in participants_raw:
            if not isinstance(raw, Mapping):
                continue
            raw_tag, _ = _read(raw, "tag", "playerTag", "player_tag")
            member = lookup.get(_safe_tag(raw_tag) or "")
            participant = _normalize_participant_mapping(raw, metadata, member=member)
            if participant is not None:
                normalized.append(participant)
        participant_rows = tuple(normalized)
    else:
        participant_rows = None
        statuses["participants"] = PRESENCE_INVALID

    if context_clan_tag is None:
        is_opponent = None
        statuses["is_opponent"] = PRESENCE_MISSING
    else:
        is_opponent = clan_tag != context_clan_tag
        statuses["is_opponent"] = PRESENCE_DERIVED

    required = ("clan_tag", "name")
    status_value = _status_for(
        metadata,
        statuses,
        required=required,
        empty=False,
    )
    object_metadata = NormalizationMetadata(
        source=metadata.source,
        captured_at=metadata.captured_at,
        fetched_at=metadata.fetched_at,
        data_status=status_value,
        is_stale=metadata.is_stale,
        stale_reason=metadata.stale_reason,
        error_code=metadata.error_code,
        status_code=metadata.status_code,
        endpoint=metadata.endpoint,
    )
    return RaceClan(
        clan_tag=clan_tag,
        name=name,
        member_count=numbers["member_count"],
        clan_type=clan_type,
        badge_id=numbers["badge_id"],
        clan_score=numbers["clan_score"],
        clan_war_trophies=numbers["clan_war_trophies"],
        rank=numbers["rank"],
        fame=numbers["fame"],
        repair_points=numbers["repair_points"],
        decks_used=numbers["decks_used"],
        decks_used_today=numbers["decks_used_today"],
        boat_attacks=numbers["boat_attacks"],
        boat_attacks_today=numbers["boat_attacks_today"],
        boat_defenses=numbers["boat_defenses"],
        boat_defenses_today=numbers["boat_defenses_today"],
        battles_played=numbers["battles_played"],
        wins=numbers["wins"],
        collection_day_battles_played=numbers["collection_day_battles_played"],
        battles_remaining=numbers["battles_remaining"],
        is_opponent=is_opponent,
        participants=participant_rows,
        metadata=object_metadata,
        field_status=statuses,
    )


_RACE_CLAN_VALUE_FIELDS = (
    "clan_tag",
    "name",
    "member_count",
    "clan_type",
    "badge_id",
    "clan_score",
    "clan_war_trophies",
    "rank",
    "fame",
    "repair_points",
    "decks_used",
    "decks_used_today",
    "boat_attacks",
    "boat_attacks_today",
    "boat_defenses",
    "boat_defenses_today",
    "battles_played",
    "wins",
    "collection_day_battles_played",
    "battles_remaining",
    "is_opponent",
    "participants",
)


def _merge_race_clans(previous: RaceClan, current: RaceClan) -> RaceClan:
    """Merge duplicate views without losing totals from either API branch."""

    values: Dict[str, Any] = {}
    for name in _RACE_CLAN_VALUE_FIELDS:
        old_value = getattr(previous, name)
        new_value = getattr(current, name)
        if name == "participants" and new_value == () and old_value is not None and old_value != ():
            values[name] = old_value
        else:
            values[name] = new_value if new_value is not None else old_value

    statuses = dict(previous.field_status)
    for name, status in current.field_status.items():
        previous_status = statuses.get(name)
        if status in {PRESENCE_MISSING, PRESENCE_NULL, PRESENCE_INVALID} and previous_status in {
            PRESENCE_PRESENT,
            PRESENCE_FALLBACK,
            PRESENCE_CONTEXT,
            PRESENCE_DERIVED,
        }:
            continue
        statuses[name] = status
    return replace(current, **values, field_status=statuses)


def normalize_race_context(
    response: Any,
    *,
    clan_tag: Optional[str] = None,
    metadata: Any = None,
    source: Optional[str] = None,
    captured_at: Any = None,
    fetched_at: Any = None,
    data_status: Optional[str] = None,
) -> RaceContext:
    """Normalize context from current-race or river-race-log JSON."""

    view = _view(
        response,
        metadata=metadata,
        source=source,
        captured_at=captured_at,
        fetched_at=fetched_at,
        data_status=data_status,
    )
    root, root_status = _race_root(view.payload)
    statuses: Dict[str, str] = {}

    if clan_tag is not None:
        value = _safe_tag(clan_tag)
        clan_tag_value = value
        statuses["clan_tag"] = PRESENCE_CONTEXT if value is not None else PRESENCE_INVALID
    else:
        raw, status = _read(root, "clanTag", "clan_tag")
        clan_tag_value, statuses["clan_tag"] = _as_tag(raw, status)
        if clan_tag_value is None:
            entries = _clan_entries(root)
            if entries:
                raw, _ = _read(entries[0][0], "tag", "clanTag", "clan_tag")
                inferred = _safe_tag(raw)
                if inferred is not None:
                    clan_tag_value = inferred
                    statuses["clan_tag"] = PRESENCE_DERIVED

    for output, *keys in (
        ("season_id", "seasonId", "season_id", "season"),
        ("section_index", "sectionIndex", "section_index"),
        ("period_index", "periodIndex", "period_index"),
    ):
        raw, status = _read(root, *keys)
        value, value_status = _as_int(raw, status)
        if output == "season_id":
            season_id = value
        elif output == "section_index":
            section_index = value
        else:
            period_index = value
        statuses[output] = value_status

    raw, status = _read(root, "periodType", "period_type")
    period_type, statuses["period_type"] = _as_text(raw, status)
    raw, status = _read(root, "state")
    if status == PRESENCE_MISSING:
        raw, status = _read(root, "raceState", "race_state")
    state, statuses["state"] = _as_text(raw, status)
    raw, status = _read(
        root,
        "createdDate",
        "createdAt",
        "created_at",
        "raceCreatedAt",
        "race_created_at",
    )
    race_created_at, statuses["race_created_at"] = _as_timestamp(raw, status)

    raw, status = _read(root, "capturedAt", "captured_at")
    if status == PRESENCE_NULL:
        # An explicit upstream null must remain distinguishable from a
        # capture timestamp supplied by the response envelope.
        statuses["captured_at"] = PRESENCE_NULL
    elif status == PRESENCE_EMPTY:
        statuses["captured_at"] = PRESENCE_EMPTY
    elif status == PRESENCE_MISSING:
        statuses["captured_at"] = PRESENCE_METADATA if view.metadata.captured_at else status
    else:
        captured_value, captured_status = _as_timestamp(raw, status)
        if captured_value is not None:
            context_metadata = NormalizationMetadata(
                source=view.metadata.source,
                captured_at=captured_value,
                fetched_at=view.metadata.fetched_at or captured_value,
                data_status=view.metadata.data_status,
                is_stale=view.metadata.is_stale,
                stale_reason=view.metadata.stale_reason,
                error_code=view.metadata.error_code,
                status_code=view.metadata.status_code,
                endpoint=view.metadata.endpoint,
            )
            view = _PayloadView(view.payload, context_metadata)
        statuses["captured_at"] = captured_status

    statuses["source"] = PRESENCE_METADATA
    statuses["data_status"] = PRESENCE_METADATA
    required = (
        "clan_tag",
        "season_id",
        "section_index",
        "period_index",
        "period_type",
        "state",
        "race_created_at",
        "captured_at",
    )
    status_value = _status_for(
        view.metadata,
        statuses,
        required=required,
        empty=(root_status in {PRESENCE_EMPTY} or not root),
    )
    object_metadata = NormalizationMetadata(
        source=view.metadata.source,
        captured_at=view.metadata.captured_at,
        fetched_at=view.metadata.fetched_at,
        data_status=status_value,
        is_stale=view.metadata.is_stale,
        stale_reason=view.metadata.stale_reason,
        error_code=view.metadata.error_code,
        status_code=view.metadata.status_code,
        endpoint=view.metadata.endpoint,
    )
    return RaceContext(
        clan_tag=clan_tag_value,
        season_id=season_id,
        section_index=section_index,
        period_index=period_index,
        period_type=period_type,
        state=state,
        race_created_at=race_created_at,
        metadata=object_metadata,
        field_status=statuses,
    )


def normalize_race_clans(
    response: Any,
    *,
    clan_tag: Optional[str] = None,
    members: Any = None,
    metadata: Any = None,
    source: Optional[str] = None,
    captured_at: Any = None,
    fetched_at: Any = None,
    data_status: Optional[str] = None,
) -> Tuple[RaceClan, ...]:
    """Normalize own and opponent clans from all repository race variants."""

    view = _view(
        response,
        metadata=metadata,
        source=source,
        captured_at=captured_at,
        fetched_at=fetched_at,
        data_status=data_status,
    )
    root, _ = _race_root(view.payload)
    context_tag = _safe_tag(clan_tag) if clan_tag is not None else None
    lookup = _participant_lookup(members, view.metadata)
    deduped: Dict[str, RaceClan] = {}
    for node, standing in _clan_entries(root):
        clan = _normalize_race_clan_mapping(
            node,
            standing,
            view.metadata,
            context_clan_tag=context_tag,
            members_lookup=lookup,
        )
        if clan is not None:
            key = clan.clan_tag or ""
            if key in deduped:
                deduped[key] = _merge_race_clans(deduped[key], clan)
            else:
                deduped[key] = clan
    return tuple(clan for key, clan in deduped.items() if key)


def normalize_race_participants(
    response: Any,
    *,
    clan_tag: Optional[str] = None,
    members: Any = None,
    metadata: Any = None,
    source: Optional[str] = None,
    captured_at: Any = None,
    fetched_at: Any = None,
    data_status: Optional[str] = None,
) -> Tuple[RaceParticipant, ...]:
    """Normalize participants for the requested clan, without inventing rows."""

    view = _view(
        response,
        metadata=metadata,
        source=source,
        captured_at=captured_at,
        fetched_at=fetched_at,
        data_status=data_status,
    )
    root, _ = _race_root(view.payload)
    selected = _select_clan_entry(_clan_entries(root), clan_tag)
    if selected is None and isinstance(root, Mapping):
        # A small direct shape used by some test fixtures: {participants: []}.
        selected = (root, None) if "participants" in root else None
    if selected is None:
        return ()
    participant_rows, status = _participant_values(*selected)
    if status in {PRESENCE_MISSING, PRESENCE_NULL, PRESENCE_EMPTY}:
        return ()
    if not isinstance(participant_rows, Sequence) or isinstance(
        participant_rows,
        (str, bytes, bytearray),
    ):
        return ()

    lookup = _participant_lookup(members, view.metadata)
    deduped: Dict[str, RaceParticipant] = {}
    for raw in participant_rows:
        if not isinstance(raw, Mapping):
            continue
        raw_tag, _ = _read(raw, "tag", "playerTag", "player_tag")
        member = lookup.get(_safe_tag(raw_tag) or "")
        participant = _normalize_participant_mapping(raw, view.metadata, member=member)
        if participant is not None and participant.player_tag is not None:
            deduped[participant.player_tag] = participant
    return tuple(deduped.values())


def normalize_race_participant(
    response: Any,
    *,
    members: Any = None,
    metadata: Any = None,
    source: Optional[str] = None,
    captured_at: Any = None,
    fetched_at: Any = None,
    data_status: Optional[str] = None,
) -> Optional[RaceParticipant]:
    """Normalize one participant item or return the first race participant."""

    view = _view(
        response,
        metadata=metadata,
        source=source,
        captured_at=captured_at,
        fetched_at=fetched_at,
        data_status=data_status,
    )
    if isinstance(view.payload, Mapping) and any(
        key in view.payload for key in ("participants", "clan", "clans", "standings", "items")
    ):
        rows = normalize_race_participants(response, members=members, metadata=view.metadata)
        return rows[0] if rows else None
    if isinstance(view.payload, Mapping):
        raw_tag, _ = _read(view.payload, "tag", "playerTag", "player_tag")
        lookup = _participant_lookup(members, view.metadata)
        member = lookup.get(_safe_tag(raw_tag) or "")
        return _normalize_participant_mapping(view.payload, view.metadata, member=member)
    return None


def normalize_clan(
    response: Any,
    *,
    clan_tag: Optional[str] = None,
    members: Any = None,
    metadata: Any = None,
    source: Optional[str] = None,
    captured_at: Any = None,
    fetched_at: Any = None,
    data_status: Optional[str] = None,
) -> RaceClan:
    """Normalize a direct ``/clans/{tag}`` clan response."""

    view = _view(
        response,
        metadata=metadata,
        source=source,
        captured_at=captured_at,
        fetched_at=fetched_at,
        data_status=data_status,
    )
    root = _profile_root(view.payload)
    if not _looks_like_clan(root) and clan_tag is not None:
        # A requested tag is context, not an invented upstream identity; it
        # is only used when the response itself is otherwise a clan object.
        root = dict(root)
        root.setdefault("tag", clan_tag)
    result = _normalize_race_clan_mapping(
        root,
        None,
        view.metadata,
        context_clan_tag=_safe_tag(clan_tag) if clan_tag is not None else None,
        members_lookup=_participant_lookup(members, view.metadata),
    )
    if result is not None:
        return result
    # A stable empty model is safer for malformed upstream JSON than an
    # uncontrolled exception.  Its field statuses retain the invalid state.
    return RaceClan(
        metadata=NormalizationMetadata(
            source=view.metadata.source,
            captured_at=view.metadata.captured_at,
            fetched_at=view.metadata.fetched_at,
            data_status=DATA_STATUS_EMPTY if not root else DATA_STATUS_INVALID,
            is_stale=view.metadata.is_stale,
            stale_reason=view.metadata.stale_reason,
            error_code=view.metadata.error_code,
            status_code=view.metadata.status_code,
            endpoint=view.metadata.endpoint,
        ),
        field_status={"clan_tag": PRESENCE_INVALID if root else PRESENCE_MISSING},
    )


def normalize_current_river_race(
    response: Any,
    *,
    clan_tag: Optional[str] = None,
    members: Any = None,
    metadata: Any = None,
    source: Optional[str] = None,
    captured_at: Any = None,
    fetched_at: Any = None,
    data_status: Optional[str] = None,
) -> NormalizedRiverRace:
    """Normalize a current-river-race or river-race-log response as one view."""

    view = _view(
        response,
        metadata=metadata,
        source=source,
        captured_at=captured_at,
        fetched_at=fetched_at,
        data_status=data_status,
    )
    context = normalize_race_context(
        response,
        clan_tag=clan_tag,
        metadata=view.metadata,
    )
    clans = normalize_race_clans(
        response,
        clan_tag=context.clan_tag,
        members=members,
        metadata=view.metadata,
    )
    participants = normalize_race_participants(
        response,
        clan_tag=context.clan_tag,
        members=members,
        metadata=view.metadata,
    )
    field_status = {
        "context": PRESENCE_PRESENT,
        "clans": PRESENCE_PRESENT if clans else PRESENCE_EMPTY,
        "participants": PRESENCE_PRESENT if participants else PRESENCE_EMPTY,
    }
    status_value = _status_for(
        view.metadata,
        field_status,
        empty=(
            view.metadata.data_status == DATA_STATUS_EMPTY
            or (not clans and not participants)
        ),
    )
    child_statuses = [
        context.data_status,
        *(clan.data_status for clan in clans),
        *(row.data_status for row in participants),
    ]
    if status_value == DATA_STATUS_FRESH:
        if DATA_STATUS_INVALID in child_statuses:
            status_value = DATA_STATUS_INVALID
        elif DATA_STATUS_ERROR in child_statuses:
            status_value = DATA_STATUS_ERROR
        elif DATA_STATUS_PARTIAL in child_statuses:
            status_value = DATA_STATUS_PARTIAL
    object_metadata = NormalizationMetadata(
        source=view.metadata.source,
        captured_at=view.metadata.captured_at,
        fetched_at=view.metadata.fetched_at,
        data_status=status_value,
        is_stale=view.metadata.is_stale,
        stale_reason=view.metadata.stale_reason,
        error_code=view.metadata.error_code,
        status_code=view.metadata.status_code,
        endpoint=view.metadata.endpoint,
    )
    return NormalizedRiverRace(
        context=context,
        clans=clans,
        participants=participants,
        metadata=object_metadata,
        field_status=field_status,
    )


# Descriptive aliases keep the public surface discoverable while retaining a
# single implementation.  The aliases are intentionally local aliases, not
# wrappers that could diverge in their handling of metadata or malformed JSON.
normalize_river_race = normalize_current_river_race
normalize_race = normalize_current_river_race
normalize_race_clan = normalize_clan
normalize_clan_response = normalize_clan
normalize_clan_members = normalize_members
normalize_member_profiles = normalize_members
normalize_player_profiles = normalize_members
normalize_player = normalize_player_profile
normalize_player_response = normalize_player_profile
normalize_race_participant_response = normalize_race_participant


def serialize_normalized(value: Any) -> Any:
    """Return a JSON-ready copy of one normalized model or nested value."""

    return _safe_json_value(value)


__all__ = [
    "CardProfile",
    "DATA_STATUS_EMPTY",
    "DATA_STATUS_ERROR",
    "DATA_STATUS_FRESH",
    "DATA_STATUS_INVALID",
    "DATA_STATUS_PARTIAL",
    "DATA_STATUS_STALE",
    "DATA_STATUS_UNKNOWN",
    "NormalizedRiverRace",
    "NormalizationMetadata",
    "PlayerProfile",
    "RaceClan",
    "RaceContext",
    "RaceParticipant",
    "normalize_clan",
    "normalize_clan_members",
    "normalize_clan_response",
    "normalize_current_river_race",
    "normalize_member_profiles",
    "normalize_members",
    "normalize_player",
    "normalize_player_profile",
    "normalize_player_profiles",
    "normalize_player_response",
    "normalize_race",
    "normalize_race_clan",
    "normalize_race_clans",
    "normalize_race_context",
    "normalize_race_participant",
    "normalize_race_participant_response",
    "normalize_race_participants",
    "normalize_river_race",
    "serialize_normalized",
]
