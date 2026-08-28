from datetime import datetime, timedelta, timezone
from collections.abc import Mapping
import math
import os
import re


WIN_SCORE = 200
LOSS_SCORE = 100
THEORETICAL_PLAYER_CAPACITY = 50
DECKS_PER_PLAYER = 4
THEORETICAL_DECK_CAPACITY = THEORETICAL_PLAYER_CAPACITY * DECKS_PER_PLAYER
# Avg/deck is scraped from UI text rounded to two decimals, so reconstructing
# decks from medals/average needs a small tolerance around the original value.
DECK_USAGE_AVERAGE_TOLERANCE = 0.15
DECK_CAPACITY_RAW_TOLERANCE = 0.25
CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}
PHASE_TRAINING = "training"
PHASE_WAR_DAY = "war_day"
PHASE_COLOSSEUM = "colosseum"
PHASE_FINISHED = "finished"
PHASE_NOT_AVAILABLE = "not_available"
PHASE_STALE = "stale"
PHASE_ERROR = "error"
PHASE_STATUSES = frozenset(
    {
        PHASE_TRAINING,
        PHASE_WAR_DAY,
        PHASE_COLOSSEUM,
        PHASE_FINISHED,
        PHASE_NOT_AVAILABLE,
        PHASE_STALE,
        PHASE_ERROR,
    }
)
DATA_STATUS_FRESH = "fresh"
DATA_STATUS_PARTIAL = "partial"
DATA_STATUS_EMPTY = "empty"
DATA_STATUS_UNKNOWN = "unknown"
DEFAULT_BOAT_SAFETY_BUFFER = 100
STRATEGY_MODES = frozenset(
    {
        "normal",
        "protect_position",
        "strategic_experiment",
    }
)
DEFAULT_STRATEGY_MODE = "normal"
DEFAULT_BOAT_ELIGIBILITY_POLICY = {
    # These defaults are deliberately conservative and transparent.  A clan
    # can override them through the already existing strategy policy input.
    "minimumCardDepth": 8,
    "minimumObservedWarReliability": 90,
    "minimumObservedWarRaces": 2,
    "eligibleRoles": ["Leader", "Co-leader", "Elder", "Member"],
}
_SECRET_ENV_NAMES = (
    "CLASH_ROYALE_API_KEY",
    "SUPABASE_SECRET_KEY",
    "SUPABASE_SERVICE_ROLE_KEY",
    "SUPABASE_PUBLISHABLE_KEY",
    "SUPABASE_ANON_KEY",
    "SUPABASE_INGEST_TOKEN",
    "WAR_MONITOR_SECRET",
    "WAR_STATUS_LEADER_SECRET",
)
DEFAULT_STRATEGY_POLICY = {
    "scoreRules": {
        "win": WIN_SCORE,
        "loss": LOSS_SCORE,
        "expectedBoat": None,
        "tieMargin": 1,
    },
    "boat": {
        "enabled": True,
        "safetyBuffer": DEFAULT_BOAT_SAFETY_BUFFER,
    },
    "boatEligibility": dict(DEFAULT_BOAT_ELIGIBILITY_POLICY),
}


def normalize_name(name):
    cleaned = re.sub(r"\s+", " ", str(name or "")).strip().lower()
    return re.sub(r"[^\w]+", "", cleaned)


def safe_number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def safe_int(value):
    number = safe_number(value)
    return int(number) if number is not None else None


def lower_confidence(current, candidate):
    current = current if current in CONFIDENCE_ORDER else "low"
    candidate = candidate if candidate in CONFIDENCE_ORDER else "low"
    return current if CONFIDENCE_ORDER[current] <= CONFIDENCE_ORDER[candidate] else candidate


def _value_from(source, *keys):
    """Read a field from mappings and normalizer-style objects safely."""

    if source is None:
        return None
    for key in keys:
        if isinstance(source, Mapping) and key in source:
            value = source.get(key)
        else:
            try:
                value = getattr(source, key)
            except AttributeError:
                continue
        if value is not None:
            return value
    return None


def _context_sources(war_phase):
    """Return a small ordered view over legacy and normalized context shapes."""

    sources = []
    if war_phase is not None:
        sources.append(war_phase)
        for key in (
            "raceContext",
            "race_context",
            "context",
            "race",
            "normalizedRace",
            "normalized_race",
            "metadata",
            "dataQuality",
            "data_quality",
        ):
            nested = _value_from(war_phase, key)
            if nested is not None:
                sources.append(nested)
    return sources


def _context_value(war_phase, *keys):
    for source in _context_sources(war_phase):
        value = _value_from(source, *keys)
        if value is not None:
            return value
    return None


def _normalized_token(value):
    if value is None:
        return ""
    return re.sub(r"[^a-z0-9]+", "", str(value).strip().lower())


def _safe_timestamp(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            parsed = None
            for fmt in ("%Y%m%dT%H%M%S.%fZ", "%Y%m%dT%H%M%SZ"):
                try:
                    parsed = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
                    break
                except (TypeError, ValueError, OverflowError):
                    continue
    else:
        parsed = None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timestamp_text(value):
    parsed = _safe_timestamp(value)
    if parsed is not None:
        return parsed.isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and value.strip():
        return None
    return None


def _explicit_bool(value):
    """Return a boolean only when the input explicitly carries one."""

    if isinstance(value, bool):
        return value
    token = _normalized_token(value)
    if token in {"true", "yes", "1"}:
        return True
    if token in {"false", "no", "0"}:
        return False
    return None


def _safe_report_text(value, maximum):
    """Keep audit/report strings bounded and free of control characters."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > maximum:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in candidate):
        return None
    if any(
        os.environ.get(name, "").strip()
        and os.environ.get(name, "").strip() in candidate
        for name in _SECRET_ENV_NAMES
    ):
        return None
    return candidate


def normalize_strategy_mode(value, default=DEFAULT_STRATEGY_MODE):
    """Normalize the three supported strategic reporting modes."""

    aliases = {
        "normal": "normal",
        "protectposition": "protect_position",
        "protect": "protect_position",
        "strategicexperiment": "strategic_experiment",
        "experiment": "strategic_experiment",
    }
    normalized_default = aliases.get(_normalized_token(default), DEFAULT_STRATEGY_MODE)
    return aliases.get(_normalized_token(value), normalized_default)


def _strategy_decisions(war_phase):
    decisions = []
    sources = _context_sources(war_phase)
    for source in list(sources):
        for key in ("policy", "strategyPolicy", "strategy_policy"):
            nested = _value_from(source, key)
            if isinstance(nested, Mapping):
                sources.append(nested)
    for source in sources:
        decision_type = _normalized_token(
            _value_from(source, "decisionType", "decision_type")
        )
        if decision_type:
            decisions.append(source)
        for key in ("leaderDecisions", "leader_decisions", "decisions"):
            raw = _value_from(source, key)
            if isinstance(raw, Mapping):
                nested = raw.get("decisions")
                raw = nested if isinstance(nested, (list, tuple)) else [raw]
            if not isinstance(raw, (list, tuple)):
                continue
            decisions.extend(item for item in raw if isinstance(item, Mapping))
        for key in ("leaderDecision", "leader_decision"):
            raw = _value_from(source, key)
            if isinstance(raw, Mapping):
                decisions.append(raw)
    return decisions


def _strategic_experiment_decision(war_phase):
    context_clan = _context_value(war_phase, "clanTag", "clan_tag")
    context_clan = str(context_clan or "").strip().lstrip("#").upper()
    for decision in _strategy_decisions(war_phase):
        decision_type = _normalized_token(
            _value_from(decision, "decisionType", "decision_type")
        )
        if decision_type != "strategicexperiment":
            continue
        decision_clan = _value_from(decision, "clanTag", "clan_tag")
        decision_clan = str(decision_clan or "").strip().lstrip("#").upper()
        if context_clan and decision_clan and context_clan != decision_clan:
            continue
        return decision
    return None


def _strategic_week_candidate(war_phase):
    candidate = {}
    decision = _strategic_experiment_decision(war_phase)
    if decision:
        candidate.update(decision)
    for source in _context_sources(war_phase):
        raw = _value_from(
            source,
            "strategicWeek",
            "strategic_week",
            "strategyWeek",
            "strategy_week",
        )
        if isinstance(raw, Mapping):
            candidate.update(raw)
        elif raw is True:
            candidate.setdefault("isStrategicWeek", True)
    direct_fields = (
        ("reason", ("reason", "strategyReason", "strategy_reason", "experimentReason", "experiment_reason")),
        ("actor", ("actor", "strategyActor", "strategy_actor", "experimentActor", "experiment_actor")),
        ("race_key", ("raceKey", "race_key", "strategyRaceKey", "strategy_race_key")),
        ("included_in_normal_analytics", ("includedInNormalAnalytics", "included_in_normal_analytics")),
        ("observed_outcome", ("observedOutcome", "observed_outcome", "experimentOutcome", "experiment_outcome")),
    )
    for target, keys in direct_fields:
        for source in _context_sources(war_phase):
            value = _value_from(source, *keys)
            if value is not None:
                candidate.setdefault(target, value)
                break
    return candidate


def _race_key_from_context(war_phase):
    explicit = _context_value(
        war_phase,
        "raceKey",
        "race_key",
        "strategyRaceKey",
        "strategy_race_key",
    )
    safe_explicit = _safe_report_text(explicit, 256)
    if safe_explicit:
        return safe_explicit

    clan_tag = _context_value(war_phase, "clanTag", "clan_tag")
    season_id = safe_int(_context_value(war_phase, "seasonId", "season_id"))
    section_index = safe_int(
        _context_value(war_phase, "sectionIndex", "section_index")
    )
    created_at = _timestamp_text(
        _context_value(
            war_phase,
            "raceCreatedAt",
            "race_created_at",
            "createdDate",
            "created_at",
        )
    )
    if season_id is None or not created_at:
        return None
    prefix = _safe_report_text(clan_tag, 32) or "race"
    parts = [prefix, str(season_id)]
    if section_index is not None:
        parts.append(str(section_index))
    parts.append(created_at)
    return ":".join(parts)[:256]


def _safe_observed_outcome(war_phase, candidate):
    raw = _value_from(
        candidate,
        "observedOutcome",
        "observed_outcome",
        "experimentOutcome",
        "experiment_outcome",
    )
    if raw is None:
        raw = _context_value(
            war_phase,
            "observedOutcome",
            "observed_outcome",
            "experimentOutcome",
            "experiment_outcome",
        )
    if isinstance(raw, str):
        return _safe_report_text(raw, 500)
    if isinstance(raw, (bool, int, float)):
        return raw
    if isinstance(raw, Mapping):
        allowed = (
            "status",
            "result",
            "sample_size",
            "sampleSize",
            "confidence",
            "data_status",
            "dataStatus",
        )
        clean = {}
        for key in allowed:
            value = raw.get(key)
            if isinstance(value, str):
                value = _safe_report_text(value, 120)
            elif not isinstance(value, (bool, int, float)) or isinstance(value, bool):
                if not isinstance(value, bool):
                    value = None
            if value is not None:
                clean[key] = value
        return clean or None
    return None


def _resolve_strategy_mode(war_phase):
    explicit = _context_value(war_phase, "strategyMode", "strategy_mode")
    if explicit is not None:
        return normalize_strategy_mode(explicit)
    candidate = _strategic_week_candidate(war_phase)
    candidate_mode = _value_from(candidate, "strategyMode", "strategy_mode")
    if candidate_mode is not None:
        return normalize_strategy_mode(candidate_mode)
    if _explicit_bool(
        _value_from(candidate, "isStrategicWeek", "is_strategic_week")
    ) is True:
        return "strategic_experiment"
    policy = _context_value(war_phase, "policy", "strategyPolicy", "strategy_policy")
    if isinstance(policy, Mapping):
        policy_mode = _value_from(policy, "strategyMode", "strategy_mode")
        if policy_mode is not None:
            return normalize_strategy_mode(policy_mode)
    if _strategic_experiment_decision(war_phase) is not None:
        return "strategic_experiment"
    return DEFAULT_STRATEGY_MODE


def build_strategic_week_metadata(war_phase=None, *, strategy_mode=None):
    """Build an explicit, fail-closed label for a strategic experiment week."""

    candidate = _strategic_week_candidate(war_phase)
    mode = normalize_strategy_mode(
        strategy_mode if strategy_mode is not None else _resolve_strategy_mode(war_phase)
    )
    if mode != "strategic_experiment":
        return {
            "label": None,
            "is_strategic_week": False,
            "strategy_mode": mode,
            "strategyMode": mode,
            "reason": None,
            "actor": None,
            "race_key": None,
            "raceKey": None,
            "included_in_normal_analytics": True,
            "includedInNormalAnalytics": True,
            "metadata_status": "not_applicable",
            "observed_outcome": None,
            "observedOutcome": None,
            "outcome_status": "not_applicable",
            "uncertainties": [],
        }

    reason = _safe_report_text(
        _value_from(candidate, "reason", "strategyReason", "strategy_reason"),
        240,
    )
    actor = _safe_report_text(
        _value_from(candidate, "actor", "strategyActor", "strategy_actor"),
        120,
    )
    race_key = _safe_report_text(
        _value_from(candidate, "raceKey", "race_key", "relatedRaceKey", "related_race_key"),
        256,
    ) or _race_key_from_context(war_phase)
    outcome = _safe_observed_outcome(war_phase, candidate)
    missing = [
        field
        for field, value in (
            ("reason", reason),
            ("actor", actor),
            ("race_key", race_key),
        )
        if value is None
    ]
    complete = not missing
    explicit_include = _explicit_bool(
        _value_from(
            candidate,
            "includedInNormalAnalytics",
            "included_in_normal_analytics",
        )
    )
    # An incomplete audit label must never silently remove a week from normal
    # analytics.  A complete strategic experiment is excluded by default,
    # while an explicit boolean can keep it included for comparison.
    included = (
        True
        if not complete
        else explicit_include
        if explicit_include is not None
        else False
    )
    uncertainties = []
    if missing:
        uncertainties.append(
            "Strategic experiment metadata incomplete; missing "
            + ", ".join(missing)
            + "."
        )
    if outcome is None:
        uncertainties.append(
            "Observed outcome is unknown; this experiment does not guarantee loose-to-win."
        )
    data_status = _normalized_token(
        _context_value(war_phase, "dataStatus", "data_status")
    )
    if data_status in {"partial", "empty", "unknown", "stale", "error", "invalid"}:
        uncertainties.append(
            f"Race data status is {data_status}; observed outcome remains uncertain."
        )
    experiment_name = _safe_report_text(
        _value_from(candidate, "experiment", "experimentName", "experiment_name"),
        120,
    ) or "loose_to_win"
    return {
        "label": "strategic_week",
        "is_strategic_week": True,
        "strategy_mode": mode,
        "strategyMode": mode,
        "reason": reason,
        "actor": actor,
        "race_key": race_key,
        "raceKey": race_key,
        "included_in_normal_analytics": included,
        "includedInNormalAnalytics": included,
        "metadata_status": "complete" if complete else "incomplete",
        "experiment": experiment_name,
        "hypothesis": (
            "Loose-to-win wordt als experiment gevolgd; de uitkomst is geen garantie."
        ),
        "observed_outcome": outcome,
        "observedOutcome": outcome,
        "outcome_status": "observed" if outcome is not None else "unknown",
        "uncertainties": list(dict.fromkeys(uncertainties)),
    }


def _valid_war_day(value):
    day = safe_int(value)
    return day if day in {1, 2, 3, 4} else None


def _is_true(value):
    if isinstance(value, bool):
        return value
    return _normalized_token(value) in {"true", "yes", "1"}


def _error_present(value):
    if isinstance(value, Mapping):
        return bool(value)
    if isinstance(value, str):
        return _normalized_token(value) not in {"", "false", "no", "none", "null", "0"}
    return bool(value)


def _status_token(value):
    return _normalized_token(value)


def _phase_status_from_context(war_phase, current):
    """Classify race phase while keeping stale/error ahead of phase guesses."""

    data_status = _status_token(
        _context_value(war_phase, "dataStatus", "data_status")
    )
    explicit_phase = _status_token(
        _context_value(war_phase, "phaseStatus", "phase_status")
    )
    status_token = data_status or explicit_phase or _status_token(
        _context_value(war_phase, "status")
    )
    is_stale = _is_true(_context_value(war_phase, "isStale", "is_stale", "stale"))
    is_error = _error_present(
        _context_value(war_phase, "error", "hasError", "has_error")
    )
    error_code = _context_value(war_phase, "errorCode", "error_code")

    if status_token in {"error", "failed", "invalid"} or is_error:
        return PHASE_ERROR, "race_context_error"
    if is_stale or status_token == "stale":
        return PHASE_STALE, "race_context_stale"
    if error_code and status_token not in {"stale"}:
        return PHASE_ERROR, "race_context_error"
    if data_status in {"partial", "empty", "unknown", "notavailable"}:
        return PHASE_NOT_AVAILABLE, "race_context_incomplete"
    if explicit_phase in PHASE_STATUSES:
        return explicit_phase, "explicit_phase_status"
    if status_token in PHASE_STATUSES:
        return status_token, "explicit_phase_status"

    state = _status_token(_context_value(war_phase, "state", "raceState", "race_state"))
    period_type = _status_token(_context_value(war_phase, "periodType", "period_type"))
    mode = _status_token(_context_value(war_phase, "mode"))
    finish_time = _safe_timestamp(
        _context_value(war_phase, "finishTime", "finish_time")
    )
    explicit_finished = _is_true(
        _context_value(war_phase, "raceFinished", "race_finished")
    )

    if explicit_finished or state in {
        "finished",
        "warended",
        "raceended",
        "ended",
        "complete",
        "completed",
    }:
        return PHASE_FINISHED, "official_finish_state"
    if state in {"notinwar", "inactive", "notstarted", "unknown"}:
        return PHASE_NOT_AVAILABLE, "official_inactive_state"
    if finish_time is not None and finish_time <= current.astimezone(timezone.utc):
        return PHASE_FINISHED, "official_finish_time"
    if period_type in {"training", "trainingday", "collection", "collectionday"}:
        return PHASE_TRAINING, "official_period_type"
    if "training" in period_type or "collection" in period_type:
        return PHASE_TRAINING, "official_period_type"
    if "colosseum" in period_type or "colosseum" in mode:
        return PHASE_COLOSSEUM, "official_colosseum_context"
    if state in {"training", "trainingday", "collection", "collectionday"}:
        return PHASE_TRAINING, "official_state"
    if "colosseum" in state:
        return PHASE_COLOSSEUM, "official_colosseum_state"
    if period_type in {"war", "warday", "battle", "battleday"}:
        return PHASE_WAR_DAY, "official_period_type"
    if "war" in state or state in {"battle", "battleday", "active"}:
        return PHASE_WAR_DAY, "official_state"
    if mode in {"colosseum", "colosseumweekend"}:
        return PHASE_COLOSSEUM, "explicit_mode"

    explicit_day = _valid_war_day(
        _context_value(war_phase, "day", "warDay", "war_day")
    )
    if explicit_day is not None:
        return PHASE_WAR_DAY, "legacy_day"
    return PHASE_NOT_AVAILABLE, "race_context_missing"


def _phase_quality(war_phase, phase_status):
    source = _context_value(war_phase, "phaseSource", "phase_source", "source")
    confidence = _context_value(war_phase, "phaseConfidence", "phase_confidence", "confidence")
    source_token = _normalized_token(source)
    confidence_token = _normalized_token(confidence)
    period_type_value = _context_value(war_phase, "periodType", "period_type")
    official_fields = any(
        (
            isinstance(period_type_value, str) and bool(period_type_value.strip()),
            safe_int(_context_value(war_phase, "periodIndex", "period_index")) is not None,
            safe_int(_context_value(war_phase, "sectionIndex", "section_index")) is not None,
            _safe_timestamp(_context_value(war_phase, "finishTime", "finish_time")) is not None,
        )
    )
    estimated_source = any(
        marker in source_token for marker in ("cwstats", "html", "fallback", "estimated", "legacy")
    )
    legacy_signal = (
        _valid_war_day(_context_value(war_phase, "day", "warDay", "war_day")) is not None
        or _normalized_token(_context_value(war_phase, "mode")) in {"riverrace", "colosseum"}
    )
    if estimated_source or confidence_token == "low":
        quality = "estimated"
    elif official_fields or source_token in {
        "royaleapi",
        "royaleapiproxy",
        "official",
        "clashapi",
    }:
        quality = "official"
    elif phase_status in {PHASE_WAR_DAY, PHASE_COLOSSEUM} and legacy_signal:
        quality = "legacy"
    else:
        quality = "unknown"
    return quality, source, confidence


def classify_race_phase(war_phase=None, now=None):
    """Return the stable T04 phase/data-quality view for any context shape."""

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    phase_status, reason = _phase_status_from_context(war_phase, current)
    quality, source, confidence = _phase_quality(war_phase, phase_status)
    if phase_status in {PHASE_WAR_DAY, PHASE_COLOSSEUM} and quality == "estimated":
        # Estimated/stale phase context must not drive a live strategy. Keep the
        # detected value separately for diagnostics without treating it as active.
        detected_status = phase_status
        phase_status = PHASE_NOT_AVAILABLE
        reason = "estimated_phase_context"
    else:
        detected_status = phase_status

    period_type = _context_value(war_phase, "periodType", "period_type")
    period_index = safe_int(_context_value(war_phase, "periodIndex", "period_index"))
    section_index = safe_int(_context_value(war_phase, "sectionIndex", "section_index"))
    finish_time_value = _context_value(war_phase, "finishTime", "finish_time")
    finish_time = _timestamp_text(finish_time_value)
    explicit_day = _valid_war_day(
        _context_value(war_phase, "day", "warDay", "war_day")
    )
    war_day = explicit_day or _valid_war_day(period_index) or _valid_war_day(section_index)
    raw_data_status = _context_value(war_phase, "dataStatus", "data_status")
    data_status = _status_token(raw_data_status)
    if data_status not in {
        DATA_STATUS_FRESH,
        DATA_STATUS_PARTIAL,
        DATA_STATUS_EMPTY,
        DATA_STATUS_UNKNOWN,
        "stale",
        "error",
        "invalid",
    }:
        data_status = DATA_STATUS_FRESH if quality in {"official", "legacy"} else DATA_STATUS_UNKNOWN
    if phase_status == PHASE_STALE:
        data_status = "stale"
    elif phase_status == PHASE_ERROR:
        data_status = "error"
    elif phase_status == PHASE_NOT_AVAILABLE and data_status == DATA_STATUS_FRESH:
        data_status = DATA_STATUS_UNKNOWN

    return {
        "phase": phase_status,
        "status": phase_status,
        "phaseStatus": phase_status,
        "phase_status": phase_status,
        "detectedPhaseStatus": detected_status,
        "detected_phase_status": detected_status,
        "phaseStatusReason": reason,
        "phase_status_reason": reason,
        "phaseDataQuality": quality,
        "phase_data_quality": quality,
        "phaseSource": source,
        "phase_source": source,
        "phaseConfidence": confidence,
        "phase_confidence": confidence,
        "dataStatus": data_status,
        "data_status": data_status,
        "periodType": period_type,
        "period_type": period_type,
        "periodIndex": period_index,
        "period_index": period_index,
        "sectionIndex": section_index,
        "section_index": section_index,
        "finishTime": finish_time,
        "finish_time": finish_time,
        "warDay": war_day,
        "war_day": war_day,
        "officialContextFields": {
            "periodType": period_type,
            "periodIndex": period_index,
            "sectionIndex": section_index,
            "finishTime": finish_time,
        },
    }


def infer_deck_capacity_from_projection(projected_medals, average_per_deck):
    projected = safe_number(projected_medals)
    average = safe_number(average_per_deck)
    if projected is None or average is None or projected <= 0 or average <= 0:
        return None

    raw_capacity = projected / average
    inferred_capacity = int(round(raw_capacity))
    if inferred_capacity < 0 or inferred_capacity > THEORETICAL_DECK_CAPACITY:
        return None
    if abs(raw_capacity - inferred_capacity) > DECK_CAPACITY_RAW_TOLERANCE:
        return None
    return inferred_capacity


def infer_decks_used_from_medals_and_average(current_medals, average_per_deck, deck_capacity):
    medals = safe_number(current_medals)
    average = safe_number(average_per_deck)
    capacity = safe_int(deck_capacity)
    if medals is None or average is None or capacity is None:
        return None
    if medals < 0 or average <= 0 or capacity < 0:
        return None

    raw_decks_used = medals / average
    inferred_decks_used = int(round(raw_decks_used))
    if inferred_decks_used < 0 or inferred_decks_used > capacity:
        return None
    if inferred_decks_used == 0:
        return 0 if medals == 0 else None

    reconstructed_average = medals / inferred_decks_used
    if abs(reconstructed_average - average) > DECK_USAGE_AVERAGE_TOLERANCE:
        return None
    return inferred_decks_used


def next_deck_reset_utc(now=None):
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    reset = current.replace(hour=10, minute=0, second=0, microsecond=0)
    if current >= reset:
        reset += timedelta(days=1)
    return reset


def build_war_context(war_phase, now=None, target_rank=1, risk_profile="balanced"):
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    reset = next_deck_reset_utc(current)
    phase = classify_race_phase(war_phase, now=current)
    input_mode = _context_value(war_phase, "mode")
    if phase["detectedPhaseStatus"] == PHASE_COLOSSEUM:
        mode = "colosseum"
    elif phase["detectedPhaseStatus"] == PHASE_TRAINING:
        mode = "training"
    else:
        mode = input_mode or "river_race"
    policy_input = _context_value(
        war_phase,
        "policy",
        "strategyPolicy",
        "strategy_policy",
    )
    policy_source = "default"
    policy = {
        "scoreRules": dict(DEFAULT_STRATEGY_POLICY["scoreRules"]),
        "boat": dict(DEFAULT_STRATEGY_POLICY["boat"]),
        "boatEligibility": dict(DEFAULT_STRATEGY_POLICY["boatEligibility"]),
    }
    if isinstance(policy_input, Mapping):
        policy_source = "configured"
        supplied_scores = policy_input.get("scoreRules") or policy_input.get("score_rules")
        if isinstance(supplied_scores, Mapping):
            policy["scoreRules"].update(supplied_scores)
        supplied_boat = policy_input.get("boat")
        if isinstance(supplied_boat, Mapping):
            policy["boat"].update(supplied_boat)
        supplied_eligibility = policy_input.get(
            "boatEligibility",
            policy_input.get("boat_eligibility"),
        )
        if isinstance(supplied_eligibility, Mapping):
            policy["boatEligibility"].update(supplied_eligibility)
    direct_scores = _context_value(war_phase, "scoreRules", "score_rules")
    if isinstance(direct_scores, Mapping):
        policy_source = "configured"
        policy["scoreRules"].update(direct_scores)
    direct_eligibility = _context_value(
        war_phase,
        "boatEligibility",
        "boat_eligibility",
    )
    if isinstance(direct_eligibility, Mapping):
        policy_source = "configured"
        policy["boatEligibility"].update(direct_eligibility)
    strategy_mode = _resolve_strategy_mode(war_phase)
    strategic_week = build_strategic_week_metadata(
        war_phase,
        strategy_mode=strategy_mode,
    )
    policy["source"] = policy_source
    policy["assumptions"] = [
        "Score rules are configured policy values; default values are used when the API does not provide a policy.",
        "Boat advice is disabled outside a verified normal war day and remains an estimated decision aid.",
        "Boat eligibility is a manual report only; no game action is performed or promised.",
    ]
    score_rules = policy["scoreRules"]
    race_finished = phase["phaseStatus"] == PHASE_FINISHED
    explicit_finished = _is_true(
        _context_value(war_phase, "raceFinished", "race_finished")
    )
    placement_frozen = race_finished or _is_true(
        _context_value(
            war_phase,
            "placementFrozenAfterFinish",
            "placement_frozen_after_finish",
        )
    )
    if explicit_finished:
        race_finished = True
        placement_frozen = True
    return {
        "mode": mode,
        "warDay": phase["warDay"],
        "now": current.isoformat(),
        "nextDeckResetUtc": reset.isoformat(),
        "secondsUntilReset": max(0, int((reset - current.astimezone(timezone.utc)).total_seconds())),
        "raceFinished": race_finished,
        "placementFrozenAfterFinish": placement_frozen,
        "targetRank": target_rank,
        "riskProfile": risk_profile,
        "strategyMode": strategy_mode,
        "strategy_mode": strategy_mode,
        "raceKey": _race_key_from_context(war_phase),
        "race_key": _race_key_from_context(war_phase),
        "boatNeed": _context_value(
            war_phase,
            "boatNeed",
            "boat_need",
            "currentBoatNeed",
            "current_boat_need",
        ),
        "boat_need": _context_value(
            war_phase,
            "boatNeed",
            "boat_need",
            "currentBoatNeed",
            "current_boat_need",
        ),
        "boatTarget": _context_value(
            war_phase,
            "boatTarget",
            "boat_target",
            "targetClan",
            "target_clan",
        ),
        "boatTargetClanId": _context_value(
            war_phase,
            "boatTargetClanId",
            "boat_target_clan_id",
            "targetClanId",
            "target_clan_id",
        ),
        "strategicWeek": strategic_week,
        "strategic_week": strategic_week,
        "scoreRules": score_rules,
        "policy": policy,
        **phase,
    }


def _clan_value(clan, *keys):
    return _value_from(clan, *keys)


def normalize_clans(clans, clan_name, players=None, finish_outlook=None):
    current_sorted = sorted(
        clans or [],
        key=lambda c: (
            safe_number(_clan_value(c, "current_medals", "currentMedals"))
            if safe_number(_clan_value(c, "current_medals", "currentMedals")) is not None
            else -1,
            normalize_name(_clan_value(c, "name")),
        ),
        reverse=True,
    )
    projected_sorted = sorted(
        clans or [],
        key=lambda c: (
            safe_number(_clan_value(c, "projected_medals", "projectedMedals"))
            if safe_number(_clan_value(c, "projected_medals", "projectedMedals")) is not None
            else -1,
            normalize_name(_clan_value(c, "name")),
        ),
        reverse=True,
    )
    current_rank_by_name = {
        normalize_name(_clan_value(c, "name")): index
        for index, c in enumerate(current_sorted, start=1)
    }
    projected_rank_by_name = {
        normalize_name(_clan_value(c, "name")): index
        for index, c in enumerate(projected_sorted, start=1)
    }
    own_key = normalize_name(clan_name)
    players = players or []
    finish_outlook = finish_outlook or {}

    player_used = 0
    player_used_known = False
    for player in players:
        used = safe_int(
            _value_from(player, "decks_used_today", "decksUsedToday", "decksUsed")
        )
        if used is None:
            continue
        player_used += max(0, used)
        player_used_known = True

    finish_battles_left = safe_int(finish_outlook.get("battles_left"))

    rows = []
    for clan in current_sorted:
        name = _clan_value(clan, "name")
        key = normalize_name(name)
        warnings = []
        raw_decks_used = _clan_value(
            clan, "decks_used_today", "decksUsedToday", "decksUsed"
        )
        raw_decks_total = _clan_value(
            clan, "decks_total_today", "decksTotalToday", "decksTotal"
        )
        raw_current_medals = _clan_value(
            clan, "current_medals", "currentMedals", "medals", "fame"
        )
        raw_today_avg = _clan_value(
            clan,
            "avg_medals_per_deck",
            "avgMedalsPerDeck",
            "todayAveragePerDeck",
        )
        raw_projected = _clan_value(clan, "projected_medals", "projectedMedals", "projected")
        raw_boat = _clan_value(clan, "boat_points", "boatPoints", "boatValueRaw")
        raw_boat_defenses_today = _clan_value(
            clan,
            "boat_defenses_today",
            "boatDefensesToday",
        )
        raw_boat_defenses_total = _clan_value(
            clan,
            "boat_defenses",
            "boatDefenses",
            "boat_defenses_total",
            "boatDefensesTotal",
        )
        raw_boat_defenses_remaining = _clan_value(
            clan,
            "boat_defenses_remaining",
            "boatDefensesRemaining",
            "defenses_remaining",
            "defensesRemaining",
        )
        raw_boat_need = _clan_value(
            clan,
            "boat_need",
            "boatNeed",
            "boat_attack_needed",
            "boatAttackNeeded",
        )
        decks_used = safe_int(raw_decks_used)
        decks_total = safe_int(raw_decks_total)
        current_medals = safe_int(raw_current_medals)
        today_avg = safe_number(raw_today_avg)
        projected = safe_int(raw_projected)
        boat_defenses_today = _safe_nonnegative_int(raw_boat_defenses_today)
        boat_defenses_total = _safe_nonnegative_int(raw_boat_defenses_total)
        boat_defenses_remaining = _safe_nonnegative_int(raw_boat_defenses_remaining)
        boat_need = _explicit_bool(raw_boat_need)
        confidence = "high"
        capacity_source = "live" if decks_total is not None else "unknown"
        deck_data_source = "api" if decks_used is not None or decks_total is not None else "unknown"
        field_sources = {
            "currentMedals": "official" if current_medals is not None else "unknown",
            "decksUsedToday": "official" if decks_used is not None else "unknown",
            "estimatedDeckCapacityToday": "official" if decks_total is not None else "unknown",
            "todayAveragePerDeck": "derived" if today_avg is not None else "unknown",
            "projected": "estimated" if projected is not None else "unknown",
            "boatValueRaw": "official" if safe_int(raw_boat) is not None else "unknown",
            "boatDefensesToday": (
                "official" if boat_defenses_today is not None else "unknown"
            ),
            "boatDefensesTotal": (
                "official" if boat_defenses_total is not None else "unknown"
            ),
            "boatDefensesRemaining": (
                "official" if boat_defenses_remaining is not None else "unknown"
            ),
        }
        estimated_fields = []

        if key == own_key and decks_used is None and player_used_known:
            decks_used = player_used
            confidence = "medium"
            deck_data_source = "api"
            capacity_source = "player_rows"
            warnings.append("Decks gebruikt geschat uit spelersrijen.")
            field_sources["decksUsedToday"] = "estimated"
            estimated_fields.append("decksUsedToday")

        if key == own_key and finish_battles_left is not None:
            if decks_used is not None and decks_total is None:
                decks_total = decks_used + finish_battles_left
                confidence = "medium"
                deck_data_source = "api"
                capacity_source = "cwstats_battles_left"
                warnings.append("Deckcapaciteit geschat uit decks gebruikt + battles left.")
                field_sources["estimatedDeckCapacityToday"] = "estimated"
                estimated_fields.append("estimatedDeckCapacityToday")
            elif decks_used is None:
                decks_total = None

        if decks_total is None:
            inferred_capacity = infer_deck_capacity_from_projection(projected, today_avg)
            if inferred_capacity is not None:
                decks_total = inferred_capacity
                capacity_source = "inferred-from-projection"
                if deck_data_source == "unknown":
                    deck_data_source = "inferred-from-projection"
                confidence = lower_confidence(confidence, "medium")
                warnings.append("Deckcapaciteit geschat uit projected score en avg/deck.")
                field_sources["estimatedDeckCapacityToday"] = "estimated"
                estimated_fields.append("estimatedDeckCapacityToday")

        if decks_used is None:
            inferred_used = infer_decks_used_from_medals_and_average(current_medals, today_avg, decks_total)
            if inferred_used is not None:
                decks_used = inferred_used
                deck_data_source = "inferred-from-medals-and-average"
                confidence = lower_confidence(confidence, "medium")
                warnings.append("Decks gebruikt afgeleid uit huidige medailles en avg/deck.")
                field_sources["decksUsedToday"] = "estimated"
                estimated_fields.append("decksUsedToday")

        capacity_label = "live" if capacity_source == "live" and decks_total is not None else "unknown"
        if decks_total is None and decks_used is not None:
            decks_total = THEORETICAL_DECK_CAPACITY
            capacity_label = "theoretical"
            confidence = lower_confidence(confidence, "low")
            capacity_source = "theoretical_50x4"
            warnings.append("Deckcapaciteit gebruikt theoretisch maximum 50 spelers x 4.")
            field_sources["estimatedDeckCapacityToday"] = "estimated"
            estimated_fields.append("estimatedDeckCapacityToday")
        elif capacity_source != "live":
            capacity_label = "estimated"

        valid_deck_bounds = True
        if decks_total is not None and decks_used is not None and decks_used > decks_total:
            valid_deck_bounds = False
            confidence = lower_confidence(confidence, "low")
            warnings.append("Decks gebruikt is groter dan capaciteit; resterende decks onbekend gezet.")

        decks_remaining = None
        if valid_deck_bounds and decks_used is not None and decks_total is not None:
            decks_remaining = max(0, decks_total - decks_used)
        if decks_remaining is None:
            confidence = lower_confidence(confidence, "low")
            if deck_data_source == "unknown":
                warnings.append("Deckdata ontbreekt en kon niet betrouwbaar worden afgeleid.")

        historical_avg = today_avg
        if projected is not None and current_medals is not None and decks_remaining is not None and decks_remaining > 0:
            historical_avg = max(0, (projected - current_medals) / decks_remaining)

        if today_avg is not None and historical_avg is not None and decks_used is not None:
            today_weight = min(0.75, decks_used / 100)
            blended = (today_weight * today_avg) + ((1 - today_weight) * historical_avg)
        else:
            blended = today_avg if today_avg is not None else historical_avg

        if today_avg is not None:
            estimated_fields.append("todayAveragePerDeck")
        if historical_avg is not None:
            estimated_fields.append("historicalAveragePerDeck")
        if blended is not None:
            estimated_fields.append("blendedAveragePerDeck")
        if projected is not None:
            estimated_fields.append("projected")
        if estimated_fields:
            confidence = lower_confidence(confidence, "medium")

        unknown_fields = [
            field
            for field, value in (
                ("currentMedals", current_medals),
                ("decksUsedToday", decks_used),
                ("estimatedDeckCapacityToday", decks_total),
                ("todayAveragePerDeck", today_avg),
                ("historicalAveragePerDeck", historical_avg),
                ("blendedAveragePerDeck", blended),
                ("projected", projected),
                ("boatValueRaw", safe_int(raw_boat)),
                ("boatDefensesToday", boat_defenses_today),
                ("boatDefensesTotal", boat_defenses_total),
                ("boatDefensesRemaining", boat_defenses_remaining),
            )
            if value is None
        ]
        if unknown_fields and "Deckdata of racewaarden ontbreken gedeeltelijk." not in warnings:
            warnings.append("Deckdata of racewaarden ontbreken gedeeltelijk.")

        rows.append({
            "id": key,
            "name": name,
            "currentRank": current_rank_by_name.get(key),
            "projectedRank": projected_rank_by_name.get(key),
            "currentMedals": current_medals,
            "decksUsedToday": decks_used,
            "estimatedDeckCapacityToday": decks_total,
            "decksRemainingToday": decks_remaining,
            "deckCapacityLabel": capacity_label,
            "deckCapacitySource": capacity_source,
            "deckDataSource": deck_data_source,
            "participantsToday": None,
            "expectedParticipantsToday": THEORETICAL_PLAYER_CAPACITY if capacity_label == "theoretical" else None,
            "todayAveragePerDeck": today_avg,
            "historicalAveragePerDeck": historical_avg,
            "blendedAveragePerDeck": blended,
            "boatAttacksToday": None,
            "boatState": "unknown",
            "boatValueRaw": safe_int(raw_boat),
            "boatValueLabel": "Boat movement",
            "boatDefensesToday": boat_defenses_today,
            "boatDefensesTotal": boat_defenses_total,
            "boatDefensesRemaining": boat_defenses_remaining,
            "boatNeed": boat_need,
            "boatDefenseStatus": (
                "complete"
                if boat_defenses_remaining is not None
                else "partial"
                if any(value is not None for value in (boat_defenses_today, boat_defenses_total))
                else "unknown"
            ),
            "dataConfidence": confidence,
            "projected": projected,
            "trophies": safe_int(_clan_value(clan, "trophies")),
            "warnings": warnings,
            "fieldSources": field_sources,
            "field_sources": field_sources,
            "estimatedFields": sorted(set(estimated_fields)),
            "estimated_fields": sorted(set(estimated_fields)),
            "unknownFields": sorted(set(unknown_fields)),
            "unknown_fields": sorted(set(unknown_fields)),
            "valueStatus": "estimated" if estimated_fields else "official",
        })

    return rows


def project_clan(clan, score_rules=None):
    score_rules = score_rules or {}
    win = safe_number(score_rules.get("win"))
    loss = safe_number(score_rules.get("loss"))
    win = WIN_SCORE if win is None else win
    loss = LOSS_SCORE if loss is None else loss
    medals = safe_number(clan.get("currentMedals"))
    remaining = safe_int(clan.get("decksRemainingToday"))
    blended = safe_number(clan.get("blendedAveragePerDeck"))
    projected = safe_int(clan.get("projected"))
    if medals is None or remaining is None or blended is None:
        unknown_fields = [
            field
            for field, value in (
                ("currentMedals", medals),
                ("decksRemainingToday", remaining),
                ("blendedAveragePerDeck", blended),
            )
            if value is None
        ]
        return {
            "clanId": clan.get("id"),
            "floorFinal": None,
            "expectedFinal": None,
            "optimisticFinal": None,
            "ceilingFinal": None,
            "expectedRemainingDecks": remaining,
            "confidence": clan.get("dataConfidence", "low"),
            "valueStatus": "unknown",
            "estimated": False,
            "estimatedFields": [],
            "unknownFields": unknown_fields,
        }

    today_avg = safe_number(clan.get("todayAveragePerDeck"))
    optimistic_avg = max(blended, blended if today_avg is None else today_avg)
    expected_final = projected if projected is not None else int(round(medals + remaining * blended))
    estimated_fields = [
        "floorFinal",
        "expectedFinal",
        "optimisticFinal",
        "ceilingFinal",
        "p10Final",
        "p50Final",
        "p90Final",
    ]
    return {
        "clanId": clan.get("id"),
        "floorFinal": int(round(medals + remaining * loss)),
        "expectedFinal": expected_final,
        "optimisticFinal": max(expected_final, int(round(medals + remaining * optimistic_avg))),
        "ceilingFinal": int(round(medals + remaining * win)),
        "expectedRemainingDecks": remaining,
        "p10Final": int(round(medals + remaining * loss)),
        "p50Final": expected_final,
        "p90Final": int(round(medals + remaining * min(win, optimistic_avg))),
        "confidence": clan.get("dataConfidence", "low"),
        "valueStatus": "estimated",
        "estimated": True,
        "estimatedFields": estimated_fields,
        "unknownFields": [],
        "expectedFinalSource": "estimated_projection" if projected is not None else "estimated_from_average",
    }


def build_projections(clans, score_rules=None):
    return [project_clan(clan, score_rules) for clan in clans]


def rank_for_score(projections, own_id, score, compare_field):
    if score is None:
        return None
    better = 0
    for projection in projections:
        if projection.get("clanId") == own_id:
            continue
        value = projection.get(compare_field)
        if value is not None and value >= score:
            better += 1
    return better + 1


def compute_rank_bounds(our_clan, projections, target_rank=1, score_rules=None):
    own_projection = next((p for p in projections if p.get("clanId") == our_clan.get("id")), None)
    if not own_projection:
        return None
    own_floor = own_projection.get("floorFinal")
    own_expected = own_projection.get("expectedFinal")
    own_ceiling = own_projection.get("ceilingFinal")
    opponent_projections = [p for p in projections if p.get("clanId") != our_clan.get("id")]
    bounds_known = (
        own_floor is not None
        and own_ceiling is not None
        and all(p.get("floorFinal") is not None and p.get("ceilingFinal") is not None for p in opponent_projections)
    )
    expected_known = (
        own_expected is not None
        and all(p.get("expectedFinal") is not None for p in opponent_projections)
    )
    if not bounds_known:
        return {
            "bestPossibleRank": None,
            "expectedRank": None,
            "worstPossibleRank": None,
            "currentRankLocked": False,
            "desiredRankMathematicallyLocked": False,
            "desiredRankProbablyLocked": False,
            "allRelevantOpponentBoundsKnown": False,
            "valueStatus": "unknown",
            "estimated": False,
            "estimatedFields": [],
            "unknownFields": ["rankBounds"],
        }

    best = 1 + sum(1 for p in opponent_projections if p.get("floorFinal") > own_ceiling)
    expected = None
    if expected_known:
        expected = 1 + sum(1 for p in opponent_projections if p.get("expectedFinal") >= own_expected)
    worst = 1 + sum(1 for p in opponent_projections if p.get("ceilingFinal") >= own_floor)
    current_locked = best is not None and best == worst

    desired_locked = False
    if target_rank and own_floor is not None:
        guaranteed_better = 0
        for projection in opponent_projections:
            if projection.get("ceilingFinal") is not None and projection.get("ceilingFinal") >= own_projection.get("floorFinal"):
                guaranteed_better += 1
        desired_locked = guaranteed_better + 1 <= target_rank

    return {
        "bestPossibleRank": best,
        "expectedRank": expected,
        "worstPossibleRank": worst,
        "currentRankLocked": current_locked,
        "desiredRankMathematicallyLocked": desired_locked,
        "desiredRankProbablyLocked": expected is not None and expected <= target_rank,
        "allRelevantOpponentBoundsKnown": True,
        "valueStatus": "estimated",
        "estimated": True,
        "estimatedFields": [
            "bestPossibleRank",
            "expectedRank",
            "worstPossibleRank",
        ],
        "unknownFields": [] if expected is not None else ["expectedRank"],
    }


def build_rank_targets(our_clan, opponents, projections, score_rules=None):
    score_rules = score_rules or {}
    win = safe_int(score_rules.get("win"))
    loss = safe_int(score_rules.get("loss"))
    tie_margin = safe_int(score_rules.get("tieMargin"))
    win = WIN_SCORE if win is None else win
    loss = LOSS_SCORE if loss is None else loss
    tie_margin = 1 if tie_margin is None else tie_margin
    current = safe_int(our_clan.get("currentMedals"))
    remaining = safe_int(our_clan.get("decksRemainingToday"))
    if current is None or remaining is None or win <= loss:
        return []

    opponent_projection = {
        projection.get("clanId"): projection
        for projection in projections
        if projection.get("clanId") != our_clan.get("id")
    }
    scores = sorted(
        [
            projection.get("expectedFinal")
            for projection in opponent_projection.values()
            if projection.get("expectedFinal") is not None
        ],
        reverse=True,
    )
    if len(scores) < len(opponents):
        return []

    target_scores = {}
    total_clans = len(opponents) + 1
    for rank in range(1, total_clans):
        idx = rank - 1
        target_scores[rank] = scores[idx] + tie_margin

    targets = []
    for rank in range(1, total_clans):
        target_score = safe_int(target_scores.get(rank))
        if target_score is None:
            continue
        required_additional = max(0, target_score - current)
        required_avg = (required_additional / remaining) if remaining > 0 else None
        if remaining <= 0:
            if target_score > current:
                wins = None
                safe_losses = None
                status = "impossible"
            else:
                wins = 0
                safe_losses = 0
                status = "projected-target"
        else:
            raw_wins = (target_score - current - remaining * loss) / (win - loss)
            wins = math.ceil(raw_wins)
            if wins > remaining:
                wins = None
                safe_losses = None
                status = "impossible"
            else:
                wins = max(0, min(remaining, wins))
                safe_losses = remaining - wins
                if wins == 0:
                    status = "projected-target"
                elif required_avg is not None and required_avg <= win:
                    status = "stretch"
                else:
                    status = "unlikely"

        targets.append({
            "rank": rank,
            "targetScore": target_score,
            "requiredAdditionalMedals": required_additional,
            "requiredAveragePerDeck": round(required_avg, 2) if required_avg is not None else None,
            "minimumWinsNeeded": wins,
            "safeLossesAllowed": safe_losses,
            "probabilityEstimate": None,
            "status": status,
            "valueStatus": "estimated",
            "estimated": True,
            "estimatedFields": [
                "targetScore",
                "requiredAdditionalMedals",
                "requiredAveragePerDeck",
            ],
        })
    return targets


def build_score_plan(our_clan, target_score, score_rules=None):
    score_rules = score_rules or {}
    win = safe_int(score_rules.get("win"))
    loss = safe_int(score_rules.get("loss"))
    tie_margin = safe_int(score_rules.get("tieMargin"))
    win = WIN_SCORE if win is None else win
    loss = LOSS_SCORE if loss is None else loss
    tie_margin = 1 if tie_margin is None else tie_margin
    current = safe_int(our_clan.get("currentMedals"))
    remaining = safe_int(our_clan.get("decksRemainingToday"))
    score = safe_int(target_score)
    if current is None or remaining is None or score is None or win <= loss:
        return None

    needed_total = score + tie_margin
    required_additional = max(0, needed_total - current)
    required_avg = (required_additional / remaining) if remaining > 0 else None
    if remaining <= 0 and needed_total > current:
        return {
            "targetScore": score,
            "requiredAdditionalMedals": required_additional,
            "requiredAveragePerDeck": None,
            "minimumWinsNeeded": None,
            "safeLossesAllowed": None,
            "status": "impossible",
            "valueStatus": "estimated",
            "estimated": True,
            "estimatedFields": [
                "requiredAdditionalMedals",
                "requiredAveragePerDeck",
                "minimumWinsNeeded",
            ],
        }
    raw_wins = (needed_total - current - remaining * loss) / (win - loss) if remaining > 0 else 0
    wins = math.ceil(raw_wins)
    if required_avg is not None and required_avg > win:
        return {
            "targetScore": score,
            "requiredAdditionalMedals": required_additional,
            "requiredAveragePerDeck": round(required_avg, 2),
            "minimumWinsNeeded": None,
            "safeLossesAllowed": None,
            "status": "impossible",
            "valueStatus": "estimated",
            "estimated": True,
            "estimatedFields": [
                "requiredAdditionalMedals",
                "requiredAveragePerDeck",
                "minimumWinsNeeded",
            ],
        }

    wins = max(0, min(remaining, wins))
    return {
        "targetScore": score,
        "requiredAdditionalMedals": required_additional,
        "requiredAveragePerDeck": round(required_avg, 2) if required_avg is not None else None,
        "minimumWinsNeeded": wins,
        "safeLossesAllowed": remaining - wins,
        "status": "safe" if wins == 0 else "stretch",
        "valueStatus": "estimated",
        "estimated": True,
        "estimatedFields": [
            "requiredAdditionalMedals",
            "requiredAveragePerDeck",
            "minimumWinsNeeded",
            "safeLossesAllowed",
        ],
    }


def infer_current_rank(our_clan, opponents):
    explicit = safe_int(our_clan.get("currentRank"))
    if explicit:
        return explicit
    current = safe_number(our_clan.get("currentMedals"))
    if current is None:
        return None
    better = 0
    for opponent in opponents:
        value = safe_number(opponent.get("currentMedals"))
        if value is not None and value > current:
            better += 1
    return better + 1


def build_protect_current_plan(our_clan, opponents, projections, score_rules=None):
    current_rank = infer_current_rank(our_clan, opponents)
    if current_rank is None:
        return None

    current = safe_number(our_clan.get("currentMedals"))
    projection_by_id = {projection.get("clanId"): projection for projection in projections}
    threat_scores = []
    for opponent in opponents:
        opponent_current = safe_number(opponent.get("currentMedals"))
        opponent_rank = safe_int(opponent.get("currentRank"))
        is_below_now = opponent_rank > current_rank if opponent_rank is not None else (
            current is not None and opponent_current is not None and opponent_current <= current
        )
        if not is_below_now:
            continue
        projection = projection_by_id.get(opponent.get("id"))
        ceiling = safe_int((projection or {}).get("ceilingFinal"))
        if ceiling is not None:
            threat_scores.append(ceiling)

    if not threat_scores:
        return None

    plan = build_score_plan(our_clan, max(threat_scores), score_rules)
    if plan:
        plan["rank"] = current_rank
    return plan


def _effective_phase_status(context):
    status = context.get("phaseStatus") or context.get("phase_status")
    if status in PHASE_STATUSES:
        return status
    if context.get("raceFinished"):
        return PHASE_FINISHED
    if context.get("mode") == "colosseum":
        return PHASE_COLOSSEUM
    if _valid_war_day(context.get("warDay")) is not None:
        return PHASE_WAR_DAY
    return PHASE_NOT_AVAILABLE


def _phase_can_drive_strategy(context):
    status = _effective_phase_status(context)
    quality = context.get("phaseDataQuality") or context.get("phase_data_quality")
    return status in {PHASE_WAR_DAY, PHASE_COLOSSEUM} and quality != "estimated"


def _player_sources(player):
    sources = [player]
    if player is None:
        return sources
    for key in (
        "account_readiness",
        "accountReadiness",
        "profile",
        "profile_metrics",
        "profileMetrics",
        "observed_war_reliability",
        "observedWarReliability",
        "war_reliability",
        "warReliability",
        "war",
    ):
        nested = _value_from(player, key)
        if isinstance(nested, Mapping):
            sources.append(nested)
            metrics = _value_from(nested, "metrics")
            if isinstance(metrics, Mapping):
                sources.append(metrics)
    return sources


def _player_value(player, *keys):
    for source in _player_sources(player):
        value = _value_from(source, *keys)
        if value is not None:
            return value
    return None


def _safe_nonnegative_int(value):
    number = safe_int(value)
    return number if number is not None and number >= 0 else None


def _extract_card_depth(player):
    level_15 = _safe_nonnegative_int(
        _player_value(
            player,
            "level_15_depth",
            "level15Depth",
            "cards_level_15_plus",
            "cardsLevel15Plus",
        )
    )
    level_16 = _safe_nonnegative_int(
        _player_value(
            player,
            "level_16_depth",
            "level16Depth",
            "cards_level_16",
            "cardsLevel16",
        )
    )
    depth = _safe_nonnegative_int(
        _player_value(
            player,
            "card_depth",
            "cardDepth",
            "cards_depth",
            "cardsDepth",
            "deck_breadth",
            "deckBreadth",
        )
    )
    source = "explicit"

    if depth is None and level_15 is not None:
        depth = level_15
        source = "level_15_depth"
    elif depth is None and level_16 is not None:
        depth = level_16
        source = "level_16_depth"

    cards = _player_value(player, "cards")
    if depth is None and isinstance(cards, (list, tuple)):
        if not cards:
            depth = 0
            level_15 = 0
            level_16 = 0
            source = "empty_cards"
        else:
            levels = []
            for card in cards:
                level = _safe_nonnegative_int(
                    _value_from(card, "normalizedLevel", "normalized_level", "level")
                )
                if level is not None:
                    levels.append(level)
            if levels:
                level_15 = sum(level >= 15 for level in levels)
                level_16 = sum(level >= 16 for level in levels)
                depth = level_15
                source = "cards"

    return {
        "card_depth": depth,
        "cardDepth": depth,
        "level_15_depth": level_15,
        "level15Depth": level_15,
        "level_16_depth": level_16,
        "level16Depth": level_16,
        "status": (
            "known"
            if depth is not None
            else "partial"
            if any(value is not None for value in (level_15, level_16))
            else "unknown"
        ),
        "source": source if depth is not None else "unknown",
        "unknown_fields": [] if depth is not None else ["card_depth"],
    }


def _extract_observed_war_reliability(player):
    reliability = _player_value(
        player,
        "reliability",
        "reliability_score",
        "reliabilityScore",
    )
    sample_size = _player_value(
        player,
        "sample_size",
        "sampleSize",
        "observed_races",
        "observedRaces",
        "observed_war_races",
        "observedWarRaces",
        "weeks_played",
        "weeksPlayed",
    )
    if reliability is None:
        direct_reliability = _value_from(
            player,
            "observed_war_reliability",
            "observedWarReliability",
            "war_reliability",
            "warReliability",
        )
        if not isinstance(direct_reliability, Mapping):
            reliability = direct_reliability
    reliability = safe_number(reliability)
    if reliability is not None and not 0 <= reliability <= 100:
        reliability = None
    sample_size = _safe_nonnegative_int(sample_size)
    return {
        "reliability": round(reliability, 2) if reliability is not None else None,
        "reliability_score": round(reliability, 2) if reliability is not None else None,
        "sample_size": sample_size,
        "sampleSize": sample_size,
        "observed_races": sample_size,
        "observedRaces": sample_size,
        "status": (
            "known"
            if reliability is not None and sample_size is not None
            else "partial"
            if reliability is not None or sample_size is not None
            else "unknown"
        ),
        "unknown_fields": [
            field
            for field, value in (
                ("reliability", reliability),
                ("sample_size", sample_size),
            )
            if value is None
        ],
    }


def _extract_defense_evidence(source):
    sources = [source]
    if isinstance(source, Mapping):
        for key in (
            "defense",
            "defenses",
            "boatDefense",
            "boat_defense",
            "boatNeed",
            "boat_need",
        ):
            nested = _value_from(source, key)
            if isinstance(nested, Mapping):
                sources.append(nested)

    def first_value(*keys):
        for item in sources:
            value = _value_from(item, *keys)
            if value is not None:
                return _safe_nonnegative_int(value)
        return None

    today = first_value("boat_defenses_today", "boatDefensesToday")
    total = first_value(
        "boat_defenses",
        "boatDefenses",
        "boat_defenses_total",
        "boatDefensesTotal",
    )
    remaining = first_value(
        "boat_defenses_remaining",
        "boatDefensesRemaining",
        "defenses_remaining",
        "defensesRemaining",
    )
    return {
        "boat_defenses_today": today,
        "boatDefensesToday": today,
        "boat_defenses_total": total,
        "boatDefensesTotal": total,
        "boat_defenses_remaining": remaining,
        "boatDefensesRemaining": remaining,
        "status": (
            "complete"
            if remaining is not None
            else "partial"
            if any(value is not None for value in (today, total))
            else "unknown"
        ),
        "unknown_fields": [
            field
            for field, value in (
                ("boat_defenses_today", today),
                ("boat_defenses_total", total),
                ("boat_defenses_remaining", remaining),
            )
            if value is None
        ],
    }


def _boat_need_value(value, *, source="explicit"):
    if isinstance(value, bool):
        return {
            "needed": value,
            "status": "needed" if value else "not_needed",
            "source": source,
            "attacks_needed": None,
            "defenses_remaining": None,
        }
    if isinstance(value, Mapping):
        explicit = _explicit_bool(
            _value_from(
                value,
                "needed",
                "need",
                "needsBoatAttack",
                "needs_boat_attack",
                "boatAttackNeeded",
                "boat_attack_needed",
            )
        )
        attacks = _safe_nonnegative_int(
            _value_from(
                value,
                "attacksNeeded",
                "attacks_needed",
                "boatAttacksNeeded",
                "boat_attacks_needed",
                "requiredBoatAttacks",
                "required_boat_attacks",
            )
        )
        defenses_remaining = _safe_nonnegative_int(
            _value_from(
                value,
                "defensesRemaining",
                "defenses_remaining",
                "boatDefensesRemaining",
                "boat_defenses_remaining",
            )
        )
        if explicit is not None:
            needed = explicit
        elif attacks is not None:
            needed = attacks > 0
        elif defenses_remaining is not None:
            needed = defenses_remaining > 0
        else:
            state = _normalized_token(_value_from(value, "status", "state", "boatState"))
            if state in {"active", "needsattack", "need", "available", "repairing"}:
                needed = True
            elif state in {"disabled", "destroyed", "complete", "notneeded", "none"}:
                needed = False
            else:
                return None
        return {
            "needed": needed,
            "status": "needed" if needed else "not_needed",
            "source": source,
            "attacks_needed": attacks,
            "defenses_remaining": defenses_remaining,
        }
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = safe_number(value)
        if number is None or number < 0:
            return None
        needed = number > 0
        return {
            "needed": needed,
            "status": "needed" if needed else "not_needed",
            "source": source,
            "attacks_needed": int(number),
            "defenses_remaining": None,
        }
    return None


def _boat_need_from_source(source, *, source_name="unknown"):
    if source is None:
        return None
    for key in (
        "boatNeed",
        "boat_need",
        "currentBoatNeed",
        "current_boat_need",
        "warNeed",
        "war_need",
        "currentWarNeed",
        "current_war_need",
        "boatAttackNeeded",
        "boat_attack_needed",
        "needsBoatAttack",
        "needs_boat_attack",
        "attacksNeeded",
        "attacks_needed",
        "boatAttacksNeeded",
        "boat_attacks_needed",
        "requiredBoatAttacks",
        "required_boat_attacks",
        "defensesRemaining",
        "defenses_remaining",
        "boatDefensesRemaining",
        "boat_defenses_remaining",
    ):
        value = _value_from(source, key)
        if value is not None:
            result = _boat_need_value(value, source=source_name)
            if result is not None:
                if key in {
                    "defensesRemaining",
                    "defenses_remaining",
                    "boatDefensesRemaining",
                    "boat_defenses_remaining",
                }:
                    result["defenses_remaining"] = _safe_nonnegative_int(value)
                return result
    result = _boat_need_value(source, source=source_name)
    if result is not None:
        return result
    return None


def _clan_identifier(source):
    return _value_from(source, "id", "clanId", "clan_id", "tag", "clanTag", "clan_tag")


def _boat_war_need(
    context,
    opponents=None,
    target_clan=None,
    war_need=None,
    our_clan=None,
):
    opponents = list(opponents or [])
    candidate_sources = []
    if war_need is not None:
        candidate_sources.append((war_need, "explicit_argument"))
    if target_clan is not None:
        candidate_sources.append((target_clan, "target_clan"))
    if our_clan is not None:
        candidate_sources.append((our_clan, "our_clan"))
    explicit_target = _context_value(
        context,
        "boatTarget",
        "boat_target",
        "targetClan",
        "target_clan",
    )
    if explicit_target is not None:
        candidate_sources.append((explicit_target, "target_context"))
    candidate_sources.append((context, "war_context"))

    target_id = _context_value(
        context,
        "boatTargetClanId",
        "boat_target_clan_id",
        "targetClanId",
        "target_clan_id",
    )
    if target_id is not None:
        for opponent in opponents:
            if str(_clan_identifier(opponent) or "") == str(target_id):
                candidate_sources.insert(0, (opponent, "target_opponent"))
                break

    for opponent in opponents:
        candidate_sources.append((opponent, "opponent"))

    for source, source_name in candidate_sources:
        result = _boat_need_from_source(source, source_name=source_name)
        if result is None:
            continue
        identifier = _clan_identifier(source)
        result["target_clan_id"] = identifier
        result["targetClanId"] = identifier
        defense = _extract_defense_evidence(source)
        if defense.get("status") == "unknown":
            fallback_sources = [target_clan, explicit_target]
            if target_id is not None:
                fallback_sources.extend(
                    opponent
                    for opponent in opponents
                    if str(_clan_identifier(opponent) or "") == str(target_id)
                )
            for fallback_source in fallback_sources:
                fallback_defense = _extract_defense_evidence(fallback_source)
                if fallback_defense.get("status") != "unknown":
                    defense = fallback_defense
                    if result.get("target_clan_id") is None:
                        result["target_clan_id"] = _clan_identifier(fallback_source)
                        result["targetClanId"] = result["target_clan_id"]
                    break
        result["defense"] = defense
        if defense.get("boat_defenses_remaining") == 0:
            result["needed"] = False
            result["status"] = "not_needed"
            result["source"] = "defense_fields"
        return result

    defense_source = target_clan or explicit_target or context
    identifier = _clan_identifier(defense_source)
    return {
        "needed": None,
        "status": "unknown",
        "source": "unknown",
        "attacks_needed": None,
        "defenses_remaining": None,
        "target_clan_id": identifier,
        "targetClanId": identifier,
        "defense": _extract_defense_evidence(defense_source),
    }


def _boat_eligibility_policy(context, policy=None):
    result = dict(DEFAULT_BOAT_ELIGIBILITY_POLICY)
    result["eligibleRoles"] = list(DEFAULT_BOAT_ELIGIBILITY_POLICY["eligibleRoles"])
    containers = [policy]
    context_policy = _context_value(
        context,
        "policy",
        "strategyPolicy",
        "strategy_policy",
    )
    containers.append(context_policy)
    containers.append(context)
    for container in containers:
        if not isinstance(container, Mapping):
            continue
        candidates = [container]
        for key in ("boatEligibility", "boat_eligibility"):
            nested = container.get(key)
            if isinstance(nested, Mapping):
                candidates.append(nested)
        for candidate in candidates:
            for key, aliases in (
                (
                    "minimumCardDepth",
                    ("minimumCardDepth", "minimum_card_depth", "minimumLevel15Depth"),
                ),
                (
                    "minimumObservedWarReliability",
                    (
                        "minimumObservedWarReliability",
                        "minimum_observed_war_reliability",
                        "minimumReliability",
                    ),
                ),
                (
                    "minimumObservedWarRaces",
                    (
                        "minimumObservedWarRaces",
                        "minimum_observed_war_races",
                        "minimumObservedRaces",
                    ),
                ),
            ):
                value = _value_from(candidate, *aliases)
                if value is not None:
                    result[key] = value
            roles = _value_from(candidate, "eligibleRoles", "eligible_roles", "allowedRoles")
            if isinstance(roles, (list, tuple)):
                result["eligibleRoles"] = [
                    str(role).strip()
                    for role in roles
                    if isinstance(role, str) and role.strip()
                ]

    depth = _safe_nonnegative_int(result.get("minimumCardDepth"))
    reliability = safe_number(result.get("minimumObservedWarReliability"))
    races = _safe_nonnegative_int(result.get("minimumObservedWarRaces"))
    result["minimumCardDepth"] = depth if depth is not None else DEFAULT_BOAT_ELIGIBILITY_POLICY["minimumCardDepth"]
    result["minimumObservedWarReliability"] = (
        reliability
        if reliability is not None and 0 <= reliability <= 100
        else DEFAULT_BOAT_ELIGIBILITY_POLICY["minimumObservedWarReliability"]
    )
    result["minimumObservedWarRaces"] = (
        races if races is not None and races > 0 else DEFAULT_BOAT_ELIGIBILITY_POLICY["minimumObservedWarRaces"]
    )
    result["eligibleRoles"] = list(result.get("eligibleRoles") or [])
    result["enabled"] = True
    for container in containers:
        if not isinstance(container, Mapping):
            continue
        boat = container.get("boat")
        if isinstance(boat, Mapping) and _explicit_bool(boat.get("enabled")) is False:
            result["enabled"] = False
        if _explicit_bool(container.get("enabled")) is False:
            result["enabled"] = False
    return result


def build_boat_eligibility_advice(
    players,
    our_clan=None,
    opponents=None,
    context=None,
    policy=None,
    target_clan=None,
    war_need=None,
):
    """Return manual boat-assignment advice without performing a game action."""

    raw_context = context or {}
    if not (
        isinstance(raw_context, Mapping)
        and (raw_context.get("phaseStatus") or raw_context.get("phase_status"))
    ):
        raw_context = build_war_context(raw_context)
    phase_status = _effective_phase_status(raw_context)
    strategy_mode = normalize_strategy_mode(
        raw_context.get("strategyMode", raw_context.get("strategy_mode"))
    )
    eligibility_policy = _boat_eligibility_policy(raw_context, policy)
    need = _boat_war_need(
        raw_context,
        opponents=opponents,
        our_clan=our_clan,
        target_clan=target_clan,
        war_need=war_need,
    )
    phase_reason = {
        PHASE_COLOSSEUM: "Bootaanvallen zijn niet relevant in Colosseum.",
        PHASE_TRAINING: "Bootaanvallen zijn niet relevant tijdens training.",
        PHASE_FINISHED: "De race is finished; bootadvies is niet beschikbaar.",
        PHASE_STALE: "Bootadvies is geblokkeerd omdat de racecontext stale is.",
        PHASE_ERROR: "Bootadvies is geblokkeerd omdat de racecontext een fout bevat.",
        PHASE_NOT_AVAILABLE: "Bootadvies is geblokkeerd omdat de racefase onbekend is.",
    }
    if phase_status == PHASE_COLOSSEUM:
        status = "not_applicable"
        top_reason = phase_reason[PHASE_COLOSSEUM]
    elif not _phase_can_drive_strategy(raw_context):
        status = "blocked"
        top_reason = phase_reason.get(
            phase_status,
            "Bootadvies is geblokkeerd omdat de racecontext niet betrouwbaar is.",
        )
    elif not eligibility_policy["enabled"]:
        status = "disabled"
        top_reason = "Bootadvies is via strategy policy uitgeschakeld."
    elif need["needed"] is True:
        status = "available"
        top_reason = "Actuele bootbehoefte is waargenomen; kandidaten blijven handmatig advies."
    elif need["needed"] is False:
        status = "not_needed"
        top_reason = "Er is volgens de beschikbare racegegevens geen actuele bootbehoefte."
    else:
        status = "unknown"
        top_reason = "Actuele bootbehoefte en/of resterende defense is onbekend."

    target_defense = need.get("defense") or _extract_defense_evidence(target_clan or raw_context)
    target_defense_complete = target_defense.get("status") == "complete"
    need["attacksNeeded"] = need.get("attacks_needed")
    need["defensesRemaining"] = need.get("defenses_remaining")
    player_reports = []
    for player in players or []:
        tag = _player_value(player, "player_tag", "playerTag", "tag")
        name = _player_value(player, "name", "playerName", "player_name")
        role = _player_value(player, "role", "playerRole", "player_role")
        role = role.strip() if isinstance(role, str) and role.strip() else None
        cards = _extract_card_depth(player)
        reliability = _extract_observed_war_reliability(player)
        player_defense = _extract_defense_evidence(player)
        defense = player_defense if player_defense["status"] != "unknown" else target_defense
        failures = []
        missing = []
        if not tag:
            missing.append("player_tag")
        if role is None:
            missing.append("role")
        else:
            allowed_roles = {
                _normalized_token(value) for value in eligibility_policy["eligibleRoles"]
            }
            if _normalized_token(role) not in allowed_roles:
                failures.append(f"Rol {role} staat niet in de boot-eligibility policy.")
        if cards["card_depth"] is None:
            missing.append("card_depth")
        elif cards["card_depth"] < eligibility_policy["minimumCardDepth"]:
            failures.append(
                f"Kaartdiepte {cards['card_depth']} ligt onder de minimumwaarde "
                f"{eligibility_policy['minimumCardDepth']}."
            )
        if reliability["reliability"] is None:
            missing.append("observed_war_reliability")
        elif reliability["reliability"] < eligibility_policy["minimumObservedWarReliability"]:
            failures.append(
                f"Observed war reliability {reliability['reliability']}% ligt onder de "
                f"minimumwaarde {eligibility_policy['minimumObservedWarReliability']}%."
            )
        if reliability["sample_size"] is None:
            missing.append("observed_war_sample_size")
        elif reliability["sample_size"] < eligibility_policy["minimumObservedWarRaces"]:
            failures.append(
                f"Observed war sample {reliability['sample_size']} ligt onder de "
                f"minimumwaarde {eligibility_policy['minimumObservedWarRaces']}."
            )
        if need["needed"] is None:
            missing.append("current_boat_need")
        elif need["needed"] is False:
            failures.append("Er is geen actuele bootbehoefte gemeld.")
        elif not target_defense_complete:
            missing.append("boat_defenses_remaining")

        if status in {"not_applicable", "blocked", "disabled"}:
            eligible = None
            eligibility_status = "not_applicable" if status == "not_applicable" else "blocked"
            reasons = [top_reason]
        elif failures:
            eligible = False
            eligibility_status = "not_eligible"
            reasons = failures
        elif missing:
            eligible = None
            eligibility_status = "unknown"
            reasons = [
                "Boot-eligibility onbekend; ontbrekende signalen zijn niet als nul gerekend: "
                + ", ".join(missing)
                + "."
            ]
        else:
            eligible = True
            eligibility_status = "eligible"
            reasons = [
                "Rol, kaartdiepte, observed war reliability, sample en actuele defense-behoefte "
                "voldoen aan de adviespolicy."
            ]
        player_reports.append(
            {
                "player_tag": tag,
                "playerTag": tag,
                "name": name,
                "role": role,
                "card_depth": cards["card_depth"],
                "cardDepth": cards["card_depth"],
                "card_depth_details": cards,
                "cardDepthDetails": cards,
                "observed_war_reliability": reliability["reliability"],
                "observedWarReliability": reliability["reliability"],
                "observed_war_reliability_details": reliability,
                "observedWarReliabilityDetails": reliability,
                "defense": defense,
                "eligible": eligible,
                "eligibility_status": eligibility_status,
                "eligibilityStatus": eligibility_status,
                "recommend": eligible is True,
                "recommendation": "consider_manual_boat_assignment" if eligible is True else None,
                "reasons": reasons,
                "missing_fields": sorted(set(missing)),
                "unknown_fields": sorted(
                    set(missing)
                    | set(cards.get("unknown_fields") or [])
                    | set(reliability.get("unknown_fields") or [])
                    | set(defense.get("unknown_fields") or [])
                ),
            }
        )

    eligible_tags = [
        report["player_tag"]
        for report in player_reports
        if report["eligible"] is True and report["player_tag"]
    ]
    unknown_fields = []
    if need["needed"] is None:
        unknown_fields.append("current_boat_need")
    if target_defense.get("status") != "complete":
        unknown_fields.extend(target_defense.get("unknown_fields") or [])
    return {
        "status": status,
        "available": status == "available",
        "phaseStatus": phase_status,
        "phase_status": phase_status,
        "strategyMode": strategy_mode,
        "strategy_mode": strategy_mode,
        "advice_only": True,
        "adviceOnly": True,
        "automatic_action": False,
        "automaticAction": False,
        "action": None,
        "recommendation_available": bool(eligible_tags),
        "recommendationAvailable": bool(eligible_tags),
        "reason": top_reason,
        "current_need": need,
        "currentNeed": need,
        "target_clan_id": need.get("target_clan_id"),
        "targetClanId": need.get("targetClanId"),
        "defense": target_defense,
        "policy": eligibility_policy,
        "eligible_player_tags": eligible_tags,
        "eligiblePlayerTags": eligible_tags,
        "players": player_reports,
        "unknown_fields": sorted(set(unknown_fields)),
        "uncertainties": [
            "Dit is een handmatig advies/rapport; de site voert geen gameactie uit.",
        ]
        + (["Defensevelden zijn gedeeltelijk of volledig onbekend."] if not target_defense_complete else []),
    }


# ``boot`` is the Dutch UI term; ``boat`` remains the API-compatible spelling.
build_boot_eligibility_advice = build_boat_eligibility_advice
build_boat_eligibility = build_boat_eligibility_advice


def _boat_result(context, target_clan, reason, attacks=None, delay_value=None):
    return {
        "recommend": False,
        "targetClanId": target_clan.get("id") if target_clan else None,
        "attacksNeeded": attacks,
        "maximumSafeBoatAttacks": 0,
        "completionProbability": None,
        "medalOpportunityCost": None,
        "estimatedOpponentDelayValue": delay_value,
        "netStrategicValue": None,
        "reason": reason,
        "phaseStatus": _effective_phase_status(context),
        "estimated": True,
        "valueStatus": "unknown",
        "adviceOnly": True,
        "advice_only": True,
        "automaticAction": False,
        "automatic_action": False,
        "action": None,
    }


def _empty_action_plan():
    """An unavailable plan must not turn unknown work into a zero plan."""

    return {
        "minimumWins": None,
        "maximumSafeLosses": None,
        "recommendedBoatAttacks": 0,
        "decksToHoldTemporarily": None,
    }


def build_strategic_experiment_report(context):
    """Describe an experiment without turning its hypothesis into a claim."""

    metadata = build_strategic_week_metadata(
        context,
        strategy_mode="strategic_experiment",
    )
    uncertainties = list(metadata.get("uncertainties") or [])
    if not uncertainties:
        uncertainties.append(
            "Een waargenomen uitkomst is contextgebonden en bewijst geen algemene werking."
        )
    return {
        "name": metadata.get("experiment", "loose_to_win"),
        "hypothesis": metadata.get(
            "hypothesis",
            "Loose-to-win wordt als experiment gevolgd; de uitkomst is geen garantie.",
        ),
        "observed_outcome": metadata.get("observed_outcome"),
        "observedOutcome": metadata.get("observedOutcome"),
        "outcome_status": metadata.get("outcome_status", "unknown"),
        "outcomeStatus": metadata.get("outcome_status", "unknown"),
        "uncertainties": list(dict.fromkeys(uncertainties)),
        "claim": "Geen garantie; rapporteer alleen waargenomen uitkomst en onzekerheden.",
        "advice_only": True,
        "adviceOnly": True,
        "automatic_action": False,
        "automaticAction": False,
    }


def _decorate_recommendation(result, context):
    strategy_mode = normalize_strategy_mode(
        context.get("strategyMode", context.get("strategy_mode"))
    )
    result["strategyMode"] = strategy_mode
    result["strategy_mode"] = strategy_mode
    result["adviceOnly"] = True
    result["advice_only"] = True
    result["automaticAction"] = False
    result["automatic_action"] = False
    result["strategicWeek"] = context.get(
        "strategicWeek",
        context.get("strategic_week"),
    )
    result["strategic_week"] = result["strategicWeek"]
    if strategy_mode == "strategic_experiment":
        result["experimentReport"] = build_strategic_experiment_report(context)
        result["experiment_report"] = result["experimentReport"]
    return result


def _blocked_recommendation(context, phase_status, reason=None):
    labels = {
        PHASE_TRAINING: (
            "Training actief",
            "Training is geen bruikbare war day voor strategieadvies.",
        ),
        PHASE_FINISHED: (
            "Race finished",
            "De race is finished; er is geen actieve strategie meer beschikbaar.",
        ),
        PHASE_STALE: (
            "Racecontext is stale",
            "Strategieadvies is geblokkeerd omdat de racecontext verouderd is.",
        ),
        PHASE_ERROR: (
            "Racecontext bevat een fout",
            "Strategieadvies is geblokkeerd omdat de racecontext niet betrouwbaar is.",
        ),
        PHASE_NOT_AVAILABLE: (
            "Racefase onbekend",
            "Strategieadvies is geblokkeerd omdat de officiële racefase ontbreekt.",
        ),
    }
    headline, summary = labels.get(
        phase_status,
        ("Onvoldoende betrouwbare data voor strategieadvies", "Geen betrouwbaar strategieadvies beschikbaar."),
    )
    if reason:
        summary = f"{summary} ({reason})."
    return _decorate_recommendation({
        "mode": "DATA_INCOMPLETE",
        "headline": headline,
        "summary": summary,
        "confidence": "low",
        "classification": "data-quality",
        "targetClanId": None,
        "targetRank": None,
        "actionPlan": _empty_action_plan(),
        "requiredAveragePerDeck": None,
        "bestPossibleRank": None,
        "expectedRank": None,
        "worstPossibleRank": None,
        "currentRankLocked": False,
        "allRelevantOpponentBoundsKnown": False,
        "rationale": [
            f"Geen hard advies: phaseStatus={phase_status}.",
        ],
        "warnings": [summary],
        "assumptions": [],
        "phaseStatus": phase_status,
        "phase_status": phase_status,
        "dataStatus": context.get("dataStatus", DATA_STATUS_UNKNOWN),
        "data_status": context.get("data_status", context.get("dataStatus", DATA_STATUS_UNKNOWN)),
        "strategyAvailable": False,
        "strategy_available": False,
        "estimated": False,
        "estimatedFields": [],
        "unknownFields": ["strategy", "actionPlan", "rankBounds"],
    }, context)


def evaluate_boat_strike(context, our_clan, target_clan, normal_battle_expected_score, boat_battle_expected_score, estimated_attacks_to_disable, estimated_opponent_score_lost, safety_buffer=100):
    phase_status = _effective_phase_status(context)
    phase_reasons = {
        PHASE_COLOSSEUM: "Bootaanvallen zijn niet beschikbaar in Colosseum.",
        PHASE_TRAINING: "Bootaanvallen zijn niet beschikbaar tijdens training.",
        PHASE_FINISHED: "De race is finished; bootaanvallen worden niet geadviseerd.",
        PHASE_STALE: "Bootadvies geblokkeerd: racecontext is stale.",
        PHASE_ERROR: "Bootadvies geblokkeerd: racecontext bevat een fout.",
        PHASE_NOT_AVAILABLE: "Bootadvies geblokkeerd: racefase is onbekend.",
    }
    if phase_status != PHASE_WAR_DAY or not _phase_can_drive_strategy(context):
        return _boat_result(
            context,
            target_clan,
            phase_reasons.get(phase_status, "Bootadvies geblokkeerd: racefase is niet bruikbaar."),
            attacks=estimated_attacks_to_disable,
            delay_value=estimated_opponent_score_lost,
        )

    policy = context.get("policy") if isinstance(context.get("policy"), Mapping) else {}
    boat_policy = policy.get("boat") if isinstance(policy.get("boat"), Mapping) else {}
    if boat_policy.get("enabled") is False:
        return _boat_result(
            context,
            target_clan,
            "Bootadvies is via strategy policy uitgeschakeld.",
            attacks=estimated_attacks_to_disable,
            delay_value=estimated_opponent_score_lost,
        )
    if estimated_attacks_to_disable is None or estimated_attacks_to_disable <= 0:
        return _boat_result(
            context,
            target_clan,
            "Benodigde boat attacks onbekend.",
            attacks=estimated_attacks_to_disable,
            delay_value=estimated_opponent_score_lost,
        )
    normal_score = safe_number(normal_battle_expected_score)
    boat_score = safe_number(boat_battle_expected_score)
    delay_value = safe_number(estimated_opponent_score_lost)
    remaining = safe_int(our_clan.get("decksRemainingToday"))
    if normal_score is None or boat_score is None or delay_value is None or remaining is None:
        return _boat_result(
            context,
            target_clan,
            "Bootadvies geblokkeerd: vereiste schattingen of resterende decks ontbreken.",
            attacks=estimated_attacks_to_disable,
            delay_value=estimated_opponent_score_lost,
        )
    if context.get("warDay") == 4 and delay_value == 0:
        return _boat_result(
            context,
            target_clan,
            "Dag 4 boat attacks hebben alleen waarde met direct effect voor reset.",
            attacks=estimated_attacks_to_disable,
            delay_value=estimated_opponent_score_lost,
        )
    opportunity = estimated_attacks_to_disable * max(0, normal_score - boat_score)
    direct_value = delay_value
    net = direct_value - opportunity
    configured_buffer = safe_number(boat_policy.get("safetyBuffer"))
    effective_buffer = configured_buffer if configured_buffer is not None else safety_buffer
    max_safe = min(remaining, estimated_attacks_to_disable)
    recommend = net > effective_buffer and max_safe >= estimated_attacks_to_disable
    return {
        "recommend": recommend,
        "targetClanId": target_clan.get("id") if target_clan else None,
        "attacksNeeded": estimated_attacks_to_disable,
        "maximumSafeBoatAttacks": max_safe if recommend else 0,
        "completionProbability": None,
        "medalOpportunityCost": opportunity,
        "estimatedOpponentDelayValue": direct_value,
        "netStrategicValue": net,
        "reason": "Boat strike netto positief." if recommend else "Boat strike heeft geen positieve veilige netto waarde.",
        "phaseStatus": phase_status,
        "estimated": True,
        "valueStatus": "estimated",
        "estimatedFields": [
            "attacksNeeded",
            "medalOpportunityCost",
            "estimatedOpponentDelayValue",
            "netStrategicValue",
        ],
        "adviceOnly": True,
        "advice_only": True,
        "automaticAction": False,
        "automatic_action": False,
        "action": None,
    }


def recommend_strategy(context, our_clan, opponents, projections, rank_targets):
    warnings = []
    assumptions = [
        "Tie-break gebruikt conservatief +1 punt omdat exacte tie-break niet beschikbaar is.",
        "Matchmakingeffect van verliezen is communityhypothese en niet exact gepubliceerd door Supercell.",
    ]
    def finish(result):
        return _decorate_recommendation(result, context)
    strategy_mode = normalize_strategy_mode(
        context.get("strategyMode", context.get("strategy_mode"))
    )
    if strategy_mode == "protect_position":
        assumptions.append(
            "Protect-position is een rapportagemodus; resterende gameacties blijven handmatig."
        )
    elif strategy_mode == "strategic_experiment":
        assumptions.append(
            "Strategic experiment rapporteert alleen waarnemingen en onzekerheden; loose-to-win is niet gegarandeerd."
        )
    phase_status = _effective_phase_status(context)
    if phase_status not in {PHASE_WAR_DAY, PHASE_COLOSSEUM}:
        return _blocked_recommendation(
            context,
            phase_status,
            context.get("phaseStatusReason") or context.get("phase_status_reason"),
        )
    if not _phase_can_drive_strategy(context):
        return _blocked_recommendation(
            context,
            PHASE_NOT_AVAILABLE,
            "racefase is alleen als geschatte of legacy-context beschikbaar",
        )
    phase_quality = context.get("phaseDataQuality") or context.get("phase_data_quality")
    policy = context.get("policy") if isinstance(context.get("policy"), Mapping) else {}
    if policy.get("source") == "default":
        assumptions.append(
            "Default strategy policy is active; no upstream policy was supplied."
        )
    assumptions.extend(policy.get("assumptions") or [])
    if phase_quality in {"estimated", "unknown"}:
        assumptions.append(
            "Racefase is niet volledig officieel bevestigd; strategie blijft geblokkeerd."
        )
    elif phase_quality == "legacy":
        assumptions.append(
            "Legacy day/mode-context is gebruikt omdat officiële fasevelden niet zijn aangeleverd."
        )
    missing = []
    for field in ("currentMedals", "decksRemainingToday"):
        if our_clan.get(field) is None:
            missing.append(field)
    if not context.get("scoreRules"):
        missing.append("scoreRules")
    if missing:
        return finish({
            "mode": "DATA_INCOMPLETE",
            "headline": "Onvoldoende betrouwbare data voor strategieadvies",
            "summary": "Essentiele velden ontbreken: " + ", ".join(missing),
            "confidence": "low",
            "classification": "data-derived",
            "targetClanId": None,
            "targetRank": None,
            "actionPlan": _empty_action_plan(),
            "requiredAveragePerDeck": None,
            "bestPossibleRank": None,
            "expectedRank": None,
            "worstPossibleRank": None,
            "currentRankLocked": False,
            "allRelevantOpponentBoundsKnown": False,
            "rationale": ["Geen harde aanbeveling omdat data ontbreekt."],
            "warnings": warnings,
            "assumptions": assumptions,
            "phaseStatus": phase_status,
            "phase_status": phase_status,
            "phaseDataQuality": phase_quality,
            "phase_data_quality": phase_quality,
            "dataStatus": context.get("dataStatus", DATA_STATUS_UNKNOWN),
            "data_status": context.get("data_status", context.get("dataStatus", DATA_STATUS_UNKNOWN)),
            "strategyAvailable": False,
            "estimated": False,
            "estimatedFields": [],
            "unknownFields": missing,
        })

    bounds = compute_rank_bounds(our_clan, projections, context.get("targetRank", 1), context.get("scoreRules"))
    if not bounds:
        return finish({
            "mode": "DATA_INCOMPLETE",
            "headline": "Onvoldoende betrouwbare projectiedata",
            "summary": "Kan rangscenario's niet berekenen.",
            "confidence": "low",
            "classification": "data-derived",
            "targetClanId": None,
            "targetRank": None,
            "actionPlan": _empty_action_plan(),
            "requiredAveragePerDeck": None,
            "bestPossibleRank": None,
            "expectedRank": None,
            "worstPossibleRank": None,
            "currentRankLocked": False,
            "allRelevantOpponentBoundsKnown": False,
            "rationale": ["Projectie ontbreekt voor onze clan."],
            "warnings": warnings,
            "assumptions": assumptions,
            "phaseStatus": phase_status,
            "phase_status": phase_status,
            "phaseDataQuality": phase_quality,
            "phase_data_quality": phase_quality,
            "dataStatus": context.get("dataStatus", DATA_STATUS_UNKNOWN),
            "data_status": context.get("data_status", context.get("dataStatus", DATA_STATUS_UNKNOWN)),
            "strategyAvailable": False,
            "estimated": False,
            "estimatedFields": [],
            "unknownFields": ["rankBounds"],
        })
    if not bounds.get("allRelevantOpponentBoundsKnown"):
        warnings.append("Niet alle relevante tegenstanderprojecties hebben geldige floor en ceiling.")
        return finish({
            "mode": "DATA_INCOMPLETE",
            "headline": "Onvoldoende betrouwbare tegenstanderdata",
            "summary": "Een mathematisch advies is geblokkeerd omdat niet alle tegenstanderbounds bekend zijn.",
            "confidence": "low",
            "classification": "data-derived",
            "targetClanId": None,
            "targetRank": None,
            "actionPlan": _empty_action_plan(),
            "requiredAveragePerDeck": None,
            "bestPossibleRank": None,
            "expectedRank": None,
            "worstPossibleRank": None,
            "currentRankLocked": False,
            "rationale": ["Geen hard advies omdat opponent bounds ontbreken."],
            "warnings": warnings,
            "assumptions": assumptions,
            "phaseStatus": phase_status,
            "phase_status": phase_status,
            "phaseDataQuality": phase_quality,
            "phase_data_quality": phase_quality,
            "dataStatus": context.get("dataStatus", DATA_STATUS_UNKNOWN),
            "data_status": context.get("data_status", context.get("dataStatus", DATA_STATUS_UNKNOWN)),
            "strategyAvailable": False,
            "estimated": False,
            "estimatedFields": [],
            "unknownFields": ["opponentBounds"],
        })

    target_rank = context.get("targetRank", 1)
    target = next((t for t in rank_targets if t.get("rank") == target_rank), None)
    best_reachable = next((t for t in rank_targets if t.get("status") != "impossible"), None)
    safe_target = next((t for t in reversed(rank_targets) if t.get("status") == "safe"), None)
    if safe_target is None:
        safe_target = next((t for t in rank_targets if t.get("minimumWinsNeeded") is not None), None)
    current_rank = infer_current_rank(our_clan, opponents)
    protect_plan = build_protect_current_plan(our_clan, opponents, projections, context.get("scoreRules"))

    mode = "CONTROLLED_PUSH"
    headline = "Gecontroleerd pushen"
    summary = "Win eerst de minimale benodigde decks en herbereken daarna."
    target_for_plan = target if target and target.get("status") != "impossible" else best_reachable

    if phase_status == PHASE_COLOSSEUM:
        mode = "COLOSSEUM_PUSH"
        headline = "Colosseum: push medailles"
        summary = "Geen normale boat- of early-finishlogica; focus op echte war-day medailles."
        if bounds.get("currentRankLocked"):
            mode = "COLOSSEUM_LOCKED_THROW"
            headline = "Colosseumpositie staat vast"
            summary = "De positie is mathematisch vast; resterende PvP-gevechten mogen verloren worden."
    elif context.get("raceFinished") and context.get("placementFrozenAfterFinish"):
        mode = "EARLY_FINISH_BALANCING"
        headline = "Early-finish balancing actief"
        summary = "De racepositie is bevroren; extra medailles verbeteren de clanpositie niet meer."
    elif bounds.get("currentRankLocked"):
        mode = "POSITION_LOCKED_THROW"
        headline = "Positie staat mathematisch vast"
        summary = "Best en worst rank zijn gelijk; alles verliezen is toegestaan."
    elif (
        target
        and target.get("status") == "impossible"
        and protect_plan
        and protect_plan.get("minimumWinsNeeded") is not None
        and current_rank is not None
        and current_rank > target_rank
        and bounds.get("bestPossibleRank") == current_rank
        and bounds.get("worstPossibleRank") is not None
        and bounds.get("worstPossibleRank") > current_rank
    ):
        mode = "PROTECT_CURRENT_POSITION"
        headline = f"Bescherm plaats {current_rank}"
        summary = (
            f"Stijgen is niet meer haalbaar; win minimaal {protect_plan.get('minimumWinsNeeded')} decks "
            f"om plaats {current_rank} conservatief te beschermen."
        )
        target_for_plan = protect_plan
    elif target and target.get("status") == "impossible":
        mode = "POSITION_UNREACHABLE"
        alt = best_reachable
        headline = f"Plaats {target_rank} niet meer haalbaar"
        if alt:
            summary = f"Alternatief: plaats {alt['rank']} vraagt gemiddeld {alt['requiredAveragePerDeck']} per deck."
        else:
            summary = "Geen hogere haalbare positie gevonden; bescherm huidige positie."
    elif target and target.get("minimumWinsNeeded") == 0 and bounds.get("desiredRankMathematicallyLocked"):
        mode = "SAFE_LOSS_WINDOW"
        headline = f"Plaats {target_rank} is veilig haalbaar"
        summary = f"Er zijn {target.get('safeLossesAllowed')} veilige losses binnen dit doel."
    elif target and target.get("safeLossesAllowed") is not None and target.get("safeLossesAllowed") <= 5:
        mode = "ALL_OUT"
        headline = f"All-out voor plaats {target_rank}"
        summary = "Er is weinig veilige lossruimte; directe medailles hebben prioriteit."

    if strategy_mode == "protect_position" and phase_status == PHASE_WAR_DAY:
        if protect_plan:
            mode = "PROTECT_CURRENT_POSITION"
            headline = f"Bescherm plaats {current_rank or 'onbekend'}"
            summary = (
                "Protect-position modus actief: gebruik de conservatieve beschermingsgrens "
                "als handmatig rapportageadvies."
            )
            target_for_plan = protect_plan
        else:
            warnings.append(
                "Protect-position modus is gekozen, maar een betrouwbare beschermingsgrens ontbreekt."
            )

    if mode in {"POSITION_LOCKED_THROW", "COLOSSEUM_LOCKED_THROW", "EARLY_FINISH_BALANCING"}:
        minimum_wins = 0
        safe_losses = safe_int(our_clan.get("decksRemainingToday"))
        required_avg = None
        target_rank_output = bounds.get("expectedRank")
    elif mode == "PROTECT_CURRENT_POSITION" and protect_plan:
        minimum_wins = protect_plan.get("minimumWinsNeeded")
        safe_losses = protect_plan.get("safeLossesAllowed")
        required_avg = protect_plan.get("requiredAveragePerDeck")
        target_rank_output = current_rank
    elif target_for_plan:
        minimum_wins = target_for_plan.get("minimumWinsNeeded")
        safe_losses = target_for_plan.get("safeLossesAllowed")
        required_avg = target_for_plan.get("requiredAveragePerDeck")
        target_rank_output = target_for_plan.get("rank")
    else:
        minimum_wins = 0
        safe_losses = 0
        required_avg = None
        target_rank_output = None

    estimated_fields = sorted(
        {
            field
            for row in [our_clan, *opponents]
            for field in row.get("estimatedFields", [])
        }
        | {
            f"{projection.get('clanId')}:{field}"
            for projection in projections
            if projection.get("estimated")
            for field in projection.get("estimatedFields", [])
        }
    )
    if bounds.get("estimated"):
        estimated_fields.append("rankBounds")
    if rank_targets:
        estimated_fields.append("rankTargets")
    estimated_fields = sorted(set(estimated_fields))
    if estimated_fields:
        warnings.append("Rangscenario's en prognoses zijn estimated en geen officiële finishwaarden.")

    rationale = []
    if target:
        if target.get("requiredAveragePerDeck") is not None:
            rationale.append(f"Plaats {target_rank} vereist gemiddeld {target['requiredAveragePerDeck']} per resterende deck.")
        if target.get("status") == "impossible":
            rationale.append(f"Plaats {target_rank} ligt boven het theoretische maximum van {WIN_SCORE} per deck.")
    if best_reachable and best_reachable != target:
        rationale.append(f"Hoogste haalbare target in ladder: plaats {best_reachable['rank']}.")
    if mode == "PROTECT_CURRENT_POSITION" and protect_plan:
        rationale.append(f"Huidige positie beschermen vraagt {protect_plan['targetScore'] + 1} medailles als conservatieve grens.")
    if mode == "EARLY_FINISH_BALANCING":
        rationale.append("Race finish en bevroren plaatsing zijn expliciet actief in de context.")
    if bounds.get("currentRankLocked"):
        rationale.append("BestPossibleRank en WorstPossibleRank zijn gelijk.")

    return finish({
        "mode": mode,
        "headline": headline,
        "summary": summary,
        "confidence": our_clan.get("dataConfidence", "low"),
        "classification": "mixed" if "THROW" in mode or "DUEL" in mode else "data-derived",
        "targetClanId": None,
        "targetRank": target_rank_output,
        "actionPlan": {
            "minimumWins": minimum_wins,
            "maximumSafeLosses": safe_losses,
            "recommendedBoatAttacks": 0,
            "decksToHoldTemporarily": 0 if mode in {"ALL_OUT", "POSITION_LOCKED_THROW"} else max(0, safe_losses),
        },
        "requiredAveragePerDeck": required_avg,
        "bestPossibleRank": bounds.get("bestPossibleRank"),
        "expectedRank": bounds.get("expectedRank"),
        "worstPossibleRank": bounds.get("worstPossibleRank"),
        "currentRankLocked": bounds.get("currentRankLocked"),
        "allRelevantOpponentBoundsKnown": bounds.get("allRelevantOpponentBoundsKnown"),
        "rationale": rationale[:3],
        "warnings": warnings,
        "assumptions": assumptions,
        "phaseStatus": phase_status,
        "phase_status": phase_status,
        "phaseDataQuality": phase_quality,
        "phase_data_quality": phase_quality,
        "dataStatus": context.get("dataStatus", DATA_STATUS_UNKNOWN),
        "data_status": context.get("data_status", context.get("dataStatus", DATA_STATUS_UNKNOWN)),
        "strategyAvailable": True,
        "estimated": bool(estimated_fields),
        "estimatedFields": estimated_fields,
        "unknownFields": [],
    })


def build_strategy_report(context, recommendation=None, boat_eligibility=None):
    """Return the additive T16 report envelope used by API callers."""

    if not (
        isinstance(context, Mapping)
        and (context.get("phaseStatus") or context.get("phase_status"))
    ):
        context = build_war_context(context or {})
    strategy_mode = normalize_strategy_mode(
        context.get("strategyMode", context.get("strategy_mode"))
    )
    strategic_week = context.get("strategicWeek") or context.get("strategic_week")
    report = {
        "strategyMode": strategy_mode,
        "strategy_mode": strategy_mode,
        "phaseStatus": _effective_phase_status(context),
        "adviceOnly": True,
        "advice_only": True,
        "automaticAction": False,
        "automatic_action": False,
        "action": None,
        "strategicWeek": strategic_week,
        "strategic_week": strategic_week,
        "recommendation": {
            "mode": recommendation.get("mode") if isinstance(recommendation, Mapping) else None,
            "headline": recommendation.get("headline") if isinstance(recommendation, Mapping) else None,
            "summary": recommendation.get("summary") if isinstance(recommendation, Mapping) else None,
            "strategyAvailable": (
                recommendation.get("strategyAvailable")
                if isinstance(recommendation, Mapping)
                else False
            ),
        },
        "boatEligibility": boat_eligibility,
        "boat_eligibility": boat_eligibility,
        "boot_eligibility": boat_eligibility,
    }
    if strategy_mode == "strategic_experiment":
        report["experimentReport"] = build_strategic_experiment_report(context)
        report["experiment_report"] = report["experimentReport"]
    return report


def _official_context_field_names(context):
    return [
        key
        for key in ("periodType", "periodIndex", "sectionIndex", "finishTime")
        if context.get(key) is not None
    ]


def _context_errors(war_phase):
    errors = _context_value(war_phase, "errors")
    if isinstance(errors, (list, tuple)):
        safe_errors = []
        for error in errors:
            if isinstance(error, Mapping):
                code = error.get("code") or error.get("errorCode") or error.get("error_code")
                if code:
                    safe_errors.append({"code": str(code)})
            elif error:
                safe_errors.append({"code": str(error)})
        return safe_errors
    code = _context_value(war_phase, "errorCode", "error_code")
    return [{"code": str(code)}] if code else []


def build_strategy_package(clans, clan_name, players, finish_outlook, war_phase, now=None):
    normalized = normalize_clans(clans, clan_name, players=players, finish_outlook=finish_outlook)
    context = build_war_context(war_phase, now=now)
    projections = build_projections(normalized, context.get("scoreRules"))
    phase_status = context.get("phaseStatus", PHASE_NOT_AVAILABLE)
    phase_quality = context.get("phaseDataQuality", "unknown")
    strategy_allowed = _phase_can_drive_strategy(context)
    own = next((clan for clan in normalized if normalize_name(clan.get("name")) == normalize_name(clan_name)), None)
    opponents = [clan for clan in normalized if not own or clan.get("id") != own.get("id")]
    boat_eligibility = build_boat_eligibility_advice(
        players=players,
        our_clan=own or {},
        opponents=opponents,
        context=context,
    )
    if not own:
        recommendation = recommend_strategy(context, {"id": None}, [], projections, [])
        strategy_report = build_strategy_report(
            context,
            recommendation,
            boat_eligibility,
        )
        data_quality = {
            "status": phase_status,
            "dataStatus": context.get("dataStatus", DATA_STATUS_UNKNOWN),
            "phaseStatus": phase_status,
            "phaseDataQuality": phase_quality,
            "confidence": "low",
            "missingFields": ["ourClan"],
            "estimatedFields": [],
            "unknownFields": ["ourClan"],
            "officialFields": _official_context_field_names(context),
            "sources": ["royaleapi", "cwstats"],
            "errors": _context_errors(war_phase),
            "strategyAvailable": False,
        }
        return {
            "warContext": context,
            "raceRows": normalized,
            "projections": projections,
            "rankTargets": [],
            "recommendation": recommendation,
            "strategyReport": strategy_report,
            "strategy_report": strategy_report,
            "boatEligibility": boat_eligibility,
            "boat_eligibility": boat_eligibility,
            "boot_eligibility": boat_eligibility,
            "dataQuality": data_quality,
            "phaseStatus": phase_status,
            "dataStatus": context.get("dataStatus", DATA_STATUS_UNKNOWN),
        }
    targets = (
        build_rank_targets(own, opponents, projections, context.get("scoreRules"))
        if strategy_allowed
        else []
    )
    recommendation = recommend_strategy(context, own, opponents, projections, targets)
    strategy_report = build_strategy_report(
        context,
        recommendation,
        boat_eligibility,
    )
    missing = []
    estimated = []
    for field in ("currentMedals", "decksRemainingToday", "blendedAveragePerDeck"):
        if own.get(field) is None:
            missing.append(field)
    if own.get("deckCapacityLabel") != "live":
        estimated.append("deckCapacity")
    if own.get("deckDataSource") not in (None, "api"):
        estimated.append("decksUsed")
    estimated.extend(own.get("estimatedFields") or [])
    if any(
        projection.get("floorFinal") is None or projection.get("ceilingFinal") is None
        for projection in projections
        if projection.get("clanId") != own.get("id")
    ):
        missing.append("opponentBounds")
    for clan in opponents:
        if clan.get("deckDataSource") not in (None, "api", "unknown"):
            estimated.append(f"{clan.get('name')}: {clan.get('deckDataSource')}")
        estimated.extend(
            f"{clan.get('name')}: {field}"
            for field in clan.get("estimatedFields") or []
        )
    if phase_status in {
        PHASE_NOT_AVAILABLE,
        PHASE_STALE,
        PHASE_ERROR,
        PHASE_FINISHED,
    }:
        missing.append("raceContext")
    if phase_quality in {"estimated", "unknown"}:
        missing.append("officialPhaseContext")
    estimated.extend(
        f"projection:{projection.get('clanId')}:{field}"
        for projection in projections
        if projection.get("estimated")
        for field in projection.get("estimatedFields") or []
    )
    official_fields = _official_context_field_names(context)
    unknown_fields = sorted(
        set(
            field
            for row in normalized
            for field in row.get("unknownFields") or []
        )
        | ({"raceContext"} if not strategy_allowed else set())
    )
    confidence = own.get("dataConfidence", "low")
    if not strategy_allowed:
        confidence = "low"
    elif phase_quality == "legacy":
        confidence = lower_confidence(confidence, "medium")
    data_quality = {
        "status": phase_status,
        "dataStatus": context.get("dataStatus", DATA_STATUS_UNKNOWN),
        "phaseStatus": phase_status,
        "phaseDataQuality": phase_quality,
        "confidence": confidence,
        "missingFields": sorted(set(missing)),
        "estimatedFields": sorted(set(estimated)),
        "unknownFields": unknown_fields,
        "officialFields": official_fields,
        "sources": ["royaleapi", "cwstats"],
        "errors": _context_errors(war_phase),
        "strategyAvailable": bool(
            recommendation.get("strategyAvailable", strategy_allowed)
        ),
    }
    return {
        "warContext": context,
        "raceRows": normalized,
        "projections": projections,
        "rankTargets": targets,
        "recommendation": recommendation,
        "strategyReport": strategy_report,
        "strategy_report": strategy_report,
        "boatEligibility": boat_eligibility,
        "boat_eligibility": boat_eligibility,
        "boot_eligibility": boat_eligibility,
        "dataQuality": data_quality,
        "phaseStatus": phase_status,
        "dataStatus": context.get("dataStatus", DATA_STATUS_UNKNOWN),
    }
