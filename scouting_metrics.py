"""Transparent T15 screening metrics for Clash Royale player scouting.

The module deliberately keeps account readiness separate from observed war
reliability.  A profile can be useful before a candidate joins, but it is not
evidence of participation in Brabant Royale wars.  Only rows supplied by the
server-side own-clan history query are accepted as war observations.

All numeric fields are nullable.  ``None`` means that a value is unknown or
not computable from the available data; it is never silently converted to
zero.  Counts that describe an empty, known collection (for example zero
level-16 cards) remain zero.
"""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
import re
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import unquote


DESTINATIONS = ("main", "BR2", "BR3", "trial", "reject")
MIN_OBSERVED_WAR_RACES = 2
DEFAULT_WAR_WINDOW = 10
PROFILE_SIGNAL_NAMES = (
    "trophies",
    "best_trophies",
    "card_level",
    "level_15_depth",
    "level_16_depth",
    "deck_breadth",
)
PROFILE_SIGNAL_WEIGHTS = {
    "trophies": 15,
    "best_trophies": 15,
    "card_level": 15,
    "level_15_depth": 20,
    "level_16_depth": 20,
    "deck_breadth": 15,
}
T15_DESTINATION_TO_DECISION_TYPE = {
    "main": "main_clan",
    "BR2": "BR2",
    "BR3": "BR3",
    # T13 has no separate trial enum.  This existing audit type records a
    # temporary placement; the exact T15 destination is also in the reason.
    "trial": "strategic_experiment",
    "reject": "reject",
}

_MISSING = object()
_INTEGER_PATTERN = re.compile(r"[+-]?\d+\Z")
_TAG_PATTERN = re.compile(r"[A-Z0-9]{1,32}\Z")


def clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    """Clamp a known finite value to a bounded interval."""

    return max(minimum, min(maximum, value))


def _value(mapping: object, *keys: str) -> object:
    if not isinstance(mapping, Mapping):
        return _MISSING
    for key in keys:
        if key in mapping:
            return mapping[key]
    return _MISSING


def _safe_int(
    value: object,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        candidate = value
    elif isinstance(value, str) and _INTEGER_PATTERN.fullmatch(value.strip()):
        try:
            candidate = int(value.strip())
        except (TypeError, ValueError, OverflowError):
            return None
    elif isinstance(value, float) and isfinite(value) and value.is_integer():
        candidate = int(value)
    else:
        return None
    if minimum is not None and candidate < minimum:
        return None
    if maximum is not None and candidate > maximum:
        return None
    return candidate


def _safe_tag(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    for _ in range(2):
        decoded = unquote(candidate)
        if decoded == candidate:
            break
        candidate = decoded
    while candidate.startswith("#"):
        candidate = candidate[1:]
    normalized = candidate.upper()
    return normalized if _TAG_PATTERN.fullmatch(normalized) else None


def _safe_text(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    result = value.strip()
    return result or None


def _scaled(
    value: Optional[float],
    minimum: float,
    maximum: float,
    points: int,
) -> Optional[float]:
    if value is None:
        return None
    return round(clamp((value - minimum) / (maximum - minimum)) * points, 2)


def _display_number(value: object) -> str:
    if value is None:
        return "unknown"
    return str(value)


def normalized_card_level(
    card: Dict[str, object],
    global_max_level: Optional[int],
) -> Optional[int]:
    """Convert a rarity-relative API card level to a displayed level.

    A malformed or missing level stays unknown.  If an API response omits a
    rarity maximum, the raw level is retained as the best available signal;
    this is explicitly marked partial by :func:`build_profile_metrics`.
    """

    if not isinstance(card, Mapping):
        return None
    level = _safe_int(_value(card, "level"), minimum=0, maximum=100)
    if level is None:
        return None
    rarity_max = _safe_int(
        _value(card, "maxLevel", "max_level"),
        minimum=1,
        maximum=100,
    )
    effective_global_max = _safe_int(
        global_max_level,
        minimum=1,
        maximum=100,
    )
    if rarity_max is None:
        return level
    return max(
        0,
        level + max(0, (effective_global_max or rarity_max) - rarity_max),
    )


def _profile_cards(player: Mapping[str, object]) -> Tuple[Optional[List[object]], str]:
    raw_cards = _value(player, "cards")
    if raw_cards is _MISSING or raw_cards is None:
        return None, "unknown"
    if not isinstance(raw_cards, (list, tuple)):
        return None, "invalid"
    return list(raw_cards), "empty" if not raw_cards else "present"


def _player_clan(player: Mapping[str, object]) -> Tuple[Optional[str], Optional[str]]:
    clan = _value(player, "clan")
    if isinstance(clan, Mapping):
        return (
            _safe_tag(_value(clan, "tag", "clan_tag")),
            _safe_text(_value(clan, "name")),
        )
    return (
        _safe_tag(_value(player, "clan_tag", "clanTag")),
        _safe_text(_value(player, "clan_name", "clanName")),
    )


def build_profile_metrics(player: Dict[str, object]) -> Dict[str, object]:
    """Build account-readiness signals from an allow-listed player profile."""

    source: Mapping[str, object] = player if isinstance(player, Mapping) else {}
    cards, cards_status = _profile_cards(source)

    known_card_maxima = [
        level
        for card in (cards or [])
        for level in [
            _safe_int(
                _value(card, "maxLevel", "max_level"),
                minimum=1,
                maximum=100,
            )
        ]
        if level is not None
    ]
    global_max_level = max(known_card_maxima) if known_card_maxima else None
    display_levels = [
        level
        for card in (cards or [])
        for level in [normalized_card_level(card, global_max_level)]
        if level is not None
    ]

    if cards is None:
        cards_total = None
        cards_level_14_plus = None
        cards_level_15_plus = None
        cards_level_16 = None
        card_level = None
        deck_breadth = None
        card_metric_status = "unknown"
    elif not cards:
        cards_total = 0
        cards_level_14_plus = 0
        cards_level_15_plus = 0
        cards_level_16 = 0
        card_level = None
        deck_breadth = 0
        card_metric_status = "empty"
    elif display_levels:
        cards_total = len(cards)
        cards_level_14_plus = sum(1 for level in display_levels if level >= 14)
        cards_level_15_plus = sum(1 for level in display_levels if level >= 15)
        cards_level_16 = sum(1 for level in display_levels if level >= 16)
        card_level = max(display_levels)
        deck_breadth = cards_level_14_plus
        card_metric_status = (
            "partial"
            if len(display_levels) < len(cards)
            or any(
                _safe_int(
                    _value(card, "maxLevel", "max_level"),
                    minimum=1,
                    maximum=100,
                )
                is None
                for card in cards
            )
            else "known"
        )
    else:
        cards_total = len(cards)
        cards_level_14_plus = None
        cards_level_15_plus = None
        cards_level_16 = None
        card_level = None
        deck_breadth = None
        card_metric_status = "unknown"

    trophies = _safe_int(_value(source, "trophies"), minimum=0, maximum=100000)
    best_trophies = _safe_int(
        _value(source, "bestTrophies", "best_trophies"),
        minimum=0,
        maximum=100000,
    )
    challenge_max_wins = _safe_int(
        _value(source, "challengeMaxWins", "challenge_max_wins"),
        minimum=0,
        maximum=100000,
    )
    battle_count = _safe_int(
        _value(source, "battleCount", "battle_count"),
        minimum=0,
        maximum=100000000,
    )
    wins = _safe_int(_value(source, "wins"), minimum=0, maximum=100000000)
    losses = _safe_int(_value(source, "losses"), minimum=0, maximum=100000000)
    total_donations = _safe_int(
        _value(source, "totalDonations", "total_donations"),
        minimum=0,
        maximum=100000000,
    )
    current_clan_tag, current_clan_name = _player_clan(source)

    components = {
        "trophies": _scaled(
            trophies,
            5000,
            9000,
            PROFILE_SIGNAL_WEIGHTS["trophies"],
        ),
        "best_trophies": _scaled(
            best_trophies,
            6000,
            9000,
            PROFILE_SIGNAL_WEIGHTS["best_trophies"],
        ),
        "card_level": _scaled(
            card_level,
            14,
            16,
            PROFILE_SIGNAL_WEIGHTS["card_level"],
        ),
        "level_15_depth": _scaled(
            cards_level_15_plus,
            0,
            50,
            PROFILE_SIGNAL_WEIGHTS["level_15_depth"],
        ),
        "level_16_depth": _scaled(
            cards_level_16,
            0,
            20,
            PROFILE_SIGNAL_WEIGHTS["level_16_depth"],
        ),
        "deck_breadth": _scaled(
            deck_breadth,
            0,
            40,
            PROFILE_SIGNAL_WEIGHTS["deck_breadth"],
        ),
        # Kept as a non-scoring compatibility signal from the former model.
        "challenge_signal": _scaled(challenge_max_wins, 0, 12, 5),
    }
    known_signals = [
        name
        for name in PROFILE_SIGNAL_NAMES
        if components.get(name) is not None
    ]
    available_weight = sum(PROFILE_SIGNAL_WEIGHTS[name] for name in known_signals)
    score = (
        round(
            sum(float(components[name]) for name in known_signals)
            / available_weight
            * 100,
            1,
        )
        if available_weight
        else None
    )
    profile_status = (
        "unknown"
        if not known_signals
        else "complete"
        if len(known_signals) == len(PROFILE_SIGNAL_NAMES)
        else "partial"
    )
    confidence = (
        "unknown"
        if not known_signals
        else "hoog"
        if len(known_signals) == len(PROFILE_SIGNAL_NAMES)
        else "middel"
        if len(known_signals) >= 4
        else "laag"
    )

    field_status = {
        "trophies": "known" if trophies is not None else "unknown",
        "best_trophies": "known" if best_trophies is not None else "unknown",
        "card_level": card_metric_status,
        "cards_level_15_plus": card_metric_status,
        "cards_level_16": card_metric_status,
        "deck_breadth": card_metric_status,
        "current_clan": "known" if current_clan_tag is not None else "unknown",
    }
    unknown_labels = {
        "trophies": "trofeeën",
        "best_trophies": "beste trofeeën",
        "card_level": "maximaal kaartniveau",
        "level_15_depth": "L15-diepte",
        "level_16_depth": "L16-diepte",
        "deck_breadth": "deckbreedte",
    }
    reasons = [
        f"Account readiness {_display_number(score)}/100 op basis van "
        f"{len(known_signals)}/{len(PROFILE_SIGNAL_NAMES)} bekende profielsignalen.",
    ]
    for name in PROFILE_SIGNAL_NAMES:
        if components[name] is None:
            reasons.append(
                f"{unknown_labels[name]}: unknown; niet als 0 meegerekend."
            )
    if current_clan_tag is None:
        reasons.append(
            "Huidige clan: unknown; niet uit ontbrekende profieldata afgeleid."
        )
    else:
        reasons.append(
            f"Huidige clan: {current_clan_name or 'onbekende naam'} "
            f"({current_clan_tag})."
        )

    return {
        "score": score,
        "score_max": 100,
        "score_reason": reasons[0],
        "components": components,
        "component_max": {
            **PROFILE_SIGNAL_WEIGHTS,
            "challenge_signal": 5,
        },
        "score_weights": dict(PROFILE_SIGNAL_WEIGHTS),
        "available_weight": available_weight,
        "sample_size": len(known_signals),
        "sample_size_max": len(PROFILE_SIGNAL_NAMES),
        "sample_unit": "profile_signals",
        "confidence": confidence,
        "data_status": profile_status,
        "reason": " ".join(reasons),
        "reasons": reasons,
        "field_status": field_status,
        "cards_total": cards_total,
        "cards_level_14_plus": cards_level_14_plus,
        "cards_level_15_plus": cards_level_15_plus,
        "cards_level_15": cards_level_15_plus,
        "cards_level_16": cards_level_16,
        "detected_max_card_level": card_level,
        "card_level": card_level,
        "deck_breadth": deck_breadth,
        "viable_decks": (
            deck_breadth // 8 if deck_breadth is not None else None
        ),
        "trophies": trophies,
        "best_trophies": best_trophies,
        "battle_count": battle_count,
        "wins": wins,
        "losses": losses,
        "challenge_max_wins": challenge_max_wins,
        "total_donations": total_donations,
        "current_clan_tag": current_clan_tag,
        "current_clan_name": current_clan_name,
        "current_clan": {
            "tag": current_clan_tag,
            "name": current_clan_name,
            "status": "known" if current_clan_tag is not None else "unknown",
        },
    }


def build_account_readiness(profile: Dict[str, object]) -> Dict[str, object]:
    """Expose the profile model under the explicit T15 account-readiness key."""

    metrics = {
        key: profile.get(key)
        for key in (
            "trophies",
            "best_trophies",
            "card_level",
            "cards_level_15_plus",
            "cards_level_16",
            "deck_breadth",
            "current_clan",
        )
    }
    return {
        "score": profile.get("score"),
        "score_max": profile.get("score_max", 100),
        "score_reason": profile.get("score_reason"),
        "sample_size": profile.get("sample_size", 0),
        "sample_size_max": profile.get(
            "sample_size_max",
            len(PROFILE_SIGNAL_NAMES),
        ),
        "sample_unit": profile.get("sample_unit", "profile_signals"),
        "confidence": profile.get("confidence", "unknown"),
        "data_status": profile.get("data_status", "unknown"),
        "reason": profile.get("reason", "Account readiness: unknown."),
        "reasons": list(profile.get("reasons") or []),
        "metrics": metrics,
    }


def _row_timestamp(row: Mapping[str, object]) -> Optional[str]:
    raw = _value(row, "race_created_at", "raceCreatedAt")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _war_contribution(row: Mapping[str, object]) -> Optional[int]:
    raw = _value(row, "contribution")
    direct = _safe_int(raw, minimum=0, maximum=1000000)
    if direct is not None:
        return direct
    fame = _safe_int(_value(row, "fame"), minimum=0, maximum=1000000)
    repair = _safe_int(
        _value(row, "repair_points", "repairPoints"),
        minimum=0,
        maximum=1000000,
    )
    if fame is not None and repair is not None:
        return fame + repair
    return None


def _war_decks(row: Mapping[str, object]) -> Optional[int]:
    return _safe_int(
        _value(row, "decks_used", "decksUsed"),
        minimum=0,
        maximum=16,
    )


def build_war_metrics(
    rows: Iterable[Dict[str, object]],
    *,
    window: int = DEFAULT_WAR_WINDOW,
) -> Dict[str, object]:
    """Build reliability from own-clan race rows only.

    Rows with no race key cannot prove that they are separate races and are
    excluded from the sample.  A row with known zero contribution is retained
    as an observed row but is not counted as a played race, matching the
    existing analytics semantics.
    """

    if (
        isinstance(window, bool)
        or not isinstance(window, int)
        or not 1 <= window <= 52
    ):
        raise ValueError("window must be an integer between 1 and 52.")

    valid_rows: List[Dict[str, object]] = []
    invalid_rows = 0
    seen_keys = set()
    try:
        source_rows = list(rows or [])
    except TypeError:
        source_rows = []
        invalid_rows = 1
    for index, raw_row in enumerate(source_rows):
        if not isinstance(raw_row, Mapping):
            invalid_rows += 1
            continue
        timestamp = _row_timestamp(raw_row)
        if timestamp is None:
            invalid_rows += 1
            continue
        row = dict(raw_row)
        player_tag = _safe_tag(_value(row, "player_tag", "playerTag"))
        key = (timestamp, player_tag) if player_tag else (timestamp, index)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        valid_rows.append(row)

    ordered = sorted(
        valid_rows,
        key=lambda row: _row_timestamp(row) or "",
    )[-window:]
    played: List[Tuple[Optional[int], Optional[int]]] = []
    unknown_participation = 0
    zero_contribution_races = 0
    for row in ordered:
        contribution = _war_contribution(row)
        decks = _war_decks(row)
        if contribution is not None and contribution > 0:
            participation: Optional[bool] = True
        elif contribution is not None and contribution == 0:
            zero_contribution_races += 1
            participation = (
                decks is not None and decks > 0 if decks is not None else None
            )
        elif decks is not None and decks > 0:
            participation = True
        else:
            participation = None
        if participation is True:
            played.append((contribution, decks))
        elif participation is None:
            unknown_participation += 1

    contributions = [value for value, _ in played if value is not None]
    decks = [value for _, value in played]
    complete_decks = bool(played) and all(value is not None for value in decks)
    complete_contributions = bool(played) and len(contributions) == len(played)
    attacks_done = (
        sum(value for value in decks if value is not None)
        if complete_decks
        else None
    )
    expected_attacks = len(played) * 16 if complete_decks else None
    missed_attacks = (
        max(0, expected_attacks - attacks_done)
        if expected_attacks is not None and attacks_done is not None
        else None
    )
    reliability = (
        round(attacks_done / expected_attacks * 100, 2)
        if expected_attacks and attacks_done is not None
        else None
    )
    average_contribution = (
        round(sum(contributions) / len(contributions), 2) if contributions else None
    )
    perfect_weeks = (
        sum(1 for decks_used in decks if decks_used == 16)
        if complete_decks
        else None
    )
    perfect_rate = (
        round(perfect_weeks / len(played) * 100, 2)
        if perfect_weeks is not None and played
        else None
    )

    raw_components = {
        "reliability": (
            round(clamp((reliability - 75) / 25) * 22, 2)
            if reliability is not None
            else None
        ),
        "contribution": (
            round(clamp((average_contribution - 1800) / 1400) * 13, 2)
            if average_contribution is not None
            else None
        ),
        "perfect_week_rate": (
            round(clamp(perfect_rate / 100) * 5, 2)
            if perfect_rate is not None
            else None
        ),
    }
    sufficient = (
        len(played) >= MIN_OBSERVED_WAR_RACES
        and reliability is not None
        and average_contribution is not None
        and missed_attacks is not None
    )
    score = (
        round(sum(float(value) for value in raw_components.values()) / 40 * 100, 1)
        if sufficient and all(value is not None for value in raw_components.values())
        else None
    )
    data_status = (
        "empty"
        if not ordered
        else "complete"
        if not unknown_participation
        and (
            not played
            or (complete_decks and complete_contributions)
        )
        else "partial"
    )
    status = "sufficient" if sufficient else "insufficient"
    if data_status == "partial" and len(played) >= MIN_OBSERVED_WAR_RACES:
        status = "unknown"
    confidence = (
        "hoog"
        if sufficient and len(played) >= 6
        else "middel"
        if sufficient
        else "laag"
        if played
        else "unknown"
    )
    reasons = [
        f"Eigen waargenomen races: {len(played)} "
        f"(sample-eenheid: own_clan_races; {len(ordered)} rijen in venster).",
    ]
    if reliability is None:
        reasons.append(
            "Reliability: unknown; onzekerheid door ontbrekende deckdata "
            "is niet als 0 gerekend."
        )
    else:
        reasons.append(f"Reliability: {reliability}% over {len(played)} gespeelde races.")
    if missed_attacks is None:
        reasons.append("Gemiste aanvallen: unknown door ontbrekende deckdata.")
    else:
        reasons.append(f"Gemiste aanvallen: {missed_attacks} over de gespeelde races.")
    if average_contribution is None:
        reasons.append("Gemiddelde bijdrage: unknown; bijdragegegevens ontbreken.")
    elif not complete_contributions:
        reasons.append(
            f"Gemiddelde bijdrage: {average_contribution} over "
            f"{len(contributions)}/{len(played)} rijen met bekende bijdrage; "
            "onzekerheid blijft bestaan."
        )
    else:
        reasons.append(f"Gemiddelde bijdrage: {average_contribution}.")
    if len(played) < MIN_OBSERVED_WAR_RACES:
        reasons.append(
            f"Onvoldoende eigen warhistorie: minimaal {MIN_OBSERVED_WAR_RACES} "
            "complete gespeelde races zijn nodig voor een vaste bestemming."
        )
    if unknown_participation:
        reasons.append(
            f"{unknown_participation} geobserveerde rij(en) hebben onbekende deelname; "
            "deze zijn niet stilzwijgend als gespeeld of gemist gerekend."
        )
    if zero_contribution_races:
        reasons.append(
            f"{zero_contribution_races} race(s) met bekende nulbijdrage tellen niet als "
            "gespeelde race in de reliability-sample."
        )
    score_reason = (
        f"Observed war score {_display_number(score)}/100 op basis van "
        f"{len(played)} eigen gespeelde races."
        if score is not None
        else "Observed war score: unknown; geen volledige score berekend bij onvoldoende of onvolledige eigen data."
    )
    reasons.append(score_reason)

    return {
        "score": score,
        "war_score": score,
        "score_max": 100,
        "score_reason": score_reason,
        "components": raw_components,
        "window_weeks": window,
        "weeks_observed": len(ordered),
        "observed_races": len(ordered),
        "weeks_played": len(played),
        "sample_size": len(played),
        "sample_size_max": window,
        "sample_unit": "own_clan_races",
        "observation_scope": "own_clan_history",
        "is_own_observation": True,
        "status": status,
        "data_status": data_status,
        "confidence": confidence,
        "sufficient": sufficient,
        "average_contribution": average_contribution,
        "average_contribution_sample_size": len(contributions),
        "attacks_done": attacks_done,
        "expected_attacks": expected_attacks,
        "missed_attacks": missed_attacks,
        "reliability": reliability,
        "perfect_weeks": perfect_weeks,
        "perfect_rate": perfect_rate,
        "zero_contribution_races": zero_contribution_races,
        "unknown_participation_rows": unknown_participation,
        "invalid_rows": invalid_rows,
        "first_observed_at": _row_timestamp(ordered[0]) if ordered else None,
        "last_observed_at": _row_timestamp(ordered[-1]) if ordered else None,
        "reason": " ".join(reasons),
        "reasons": reasons,
        "uncertainty": [
            reason
            for reason in reasons
            if "unknown" in reason.lower()
            or "onvoldoende" in reason.lower()
            or "onzekerheid" in reason.lower()
        ],
    }


def build_observed_war_reliability(war: Dict[str, object]) -> Dict[str, object]:
    """Expose the war model under the explicit T15 reliability key."""

    metrics = {
        key: war.get(key)
        for key in (
            "reliability",
            "missed_attacks",
            "average_contribution",
            "observed_races",
            "weeks_played",
            "perfect_rate",
        )
    }
    return {
        "score": war.get("score"),
        "score_max": war.get("score_max", 100),
        "score_reason": war.get("score_reason"),
        "sample_size": war.get("sample_size", 0),
        "sample_size_max": war.get("sample_size_max", DEFAULT_WAR_WINDOW),
        "sample_unit": "own_clan_races",
        "observation_scope": "own_clan_history",
        "is_own_observation": True,
        "status": war.get("status", "unknown"),
        "data_status": war.get("data_status", "unknown"),
        "confidence": war.get("confidence", "unknown"),
        "sufficient": bool(war.get("sufficient")),
        "reason": war.get("reason", "Observed war reliability: unknown."),
        "reasons": list(war.get("reasons") or []),
        "uncertainty": list(war.get("uncertainty") or []),
        "metrics": metrics,
    }


def build_trial_status(
    war: Dict[str, object],
    *,
    required_races: int = MIN_OBSERVED_WAR_RACES,
) -> Dict[str, object]:
    """Describe whether the operational trial gate has been met."""

    if (
        isinstance(required_races, bool)
        or not isinstance(required_races, int)
        or not 1 <= required_races <= 52
    ):
        raise ValueError("required_races must be an integer between 1 and 52.")
    sample = _safe_int(war.get("sample_size"), minimum=0, maximum=52)
    sample = 0 if sample is None else sample
    observed = _safe_int(war.get("observed_races"), minimum=0, maximum=52)
    observed = sample if observed is None else observed
    complete = bool(war.get("sufficient")) and sample >= required_races
    if complete:
        status = "complete"
        reason = (
            f"Proefperiode afgerond: {sample} complete eigen races, "
            f"minimaal {required_races} vereist."
        )
    elif sample == 0:
        status = "required"
        reason = (
            f"Proefperiode verplicht: {observed} eigen warrijen geobserveerd, "
            "maar nog geen complete gespeelde eigen races; "
            f"minimaal {required_races} races vereist."
        )
    else:
        status = "in_progress"
        if sample >= required_races:
            reason = (
                f"Proefperiode blijft open: {sample} races zijn geobserveerd, "
                "maar minstens één benodigde war-metric is incompleet."
            )
        else:
            reason = (
                f"Proefperiode loopt: {sample}/{required_races} complete eigen races "
                "beschikbaar; vaste bestemming is nog niet gerechtvaardigd."
            )
    remaining = max(0, required_races - sample)
    return {
        "status": status,
        "required_races": required_races,
        "observed_races": observed,
        "remaining_races": remaining,
        "sample_size": sample,
        "sample_unit": "own_clan_races",
        "reason": reason,
        "reasons": [reason],
        "is_required": not complete,
        "data_status": "complete" if complete else war.get("data_status", "unknown"),
    }


def _destination_reason(
    destination: str,
    *,
    profile_score: Optional[float],
    reliability: Optional[float],
    average_contribution: Optional[float],
    sample_size: int,
    war_sufficient: bool,
) -> List[str]:
    reasons = [f"Aanbevolen bestemming: {destination}."]
    reasons.append(
        f"Samplegrootte: {sample_size} eigen waargenomen races "
        "(own_clan_races)."
    )
    if profile_score is None:
        reasons.append(
            "Account readiness: unknown; ontbrekende profielvelden zijn niet als 0 gerekend."
        )
    else:
        reasons.append(f"Account readiness: {profile_score}/100.")
    if reliability is None:
        reasons.append("Observed war reliability: unknown.")
    else:
        reasons.append(f"Observed war reliability: {reliability}%.")
    if average_contribution is None:
        reasons.append("Gemiddelde bijdrage: unknown.")
    else:
        reasons.append(f"Gemiddelde bijdrage: {average_contribution}.")
    if not war_sufficient:
        reasons.append(
            f"Minimaal {MIN_OBSERVED_WAR_RACES} complete eigen races zijn nodig; "
            "daarom blijft de bestemming trial."
        )
    return reasons


def recommend_destination(
    profile: Dict[str, object],
    war: Dict[str, object],
) -> Dict[str, object]:
    """Recommend exactly one T15 destination with reasons and sample size."""

    profile_score = profile.get("score")
    profile_score = (
        float(profile_score)
        if isinstance(profile_score, (int, float))
        else None
    )
    reliability = war.get("reliability")
    reliability = (
        float(reliability)
        if isinstance(reliability, (int, float))
        else None
    )
    average_contribution = war.get("average_contribution")
    average_contribution = (
        float(average_contribution)
        if isinstance(average_contribution, (int, float))
        else None
    )
    sample_size = _safe_int(war.get("sample_size"), minimum=0, maximum=52) or 0
    war_sufficient = (
        bool(war.get("sufficient"))
        and sample_size >= MIN_OBSERVED_WAR_RACES
    )

    if not war_sufficient:
        destination = "trial"
        score = profile_score
        score_basis = "account_readiness_only" if score is not None else "unavailable"
        confidence = "laag"
    elif reliability is not None and reliability < 70:
        destination = "reject"
        score = (
            round(profile_score * 0.4 + float(war["score"]) * 0.6, 1)
            if profile_score is not None
            and isinstance(war.get("score"), (int, float))
            else None
        )
        score_basis = (
            "account_readiness_plus_own_war" if score is not None else "unavailable"
        )
        confidence = "hoog" if sample_size >= 6 else "middel"
    elif profile_score is None:
        # Sufficient war evidence cannot replace missing account-readiness
        # evidence.  Keep the permanent destination conservative unless the
        # explicit reliability reject rule above applies.
        destination = "trial"
        score = None
        score_basis = "unavailable"
        confidence = "laag"
    elif (
        profile_score is not None
        and profile_score >= 75
        and reliability is not None
        and reliability >= 95
        and average_contribution is not None
        and average_contribution >= 2600
    ):
        destination = "main"
        score = (
            round(profile_score * 0.4 + float(war["score"]) * 0.6, 1)
            if isinstance(war.get("score"), (int, float))
            else None
        )
        score_basis = (
            "account_readiness_plus_own_war" if score is not None else "unavailable"
        )
        confidence = "hoog" if sample_size >= 6 else "middel"
    elif (
        profile_score is not None
        and profile_score >= 55
        and reliability is not None
        and reliability >= 90
        and average_contribution is not None
        and average_contribution >= 2200
    ):
        destination = "BR2"
        score = (
            round(profile_score * 0.4 + float(war["score"]) * 0.6, 1)
            if isinstance(war.get("score"), (int, float))
            else None
        )
        score_basis = (
            "account_readiness_plus_own_war" if score is not None else "unavailable"
        )
        confidence = "hoog" if sample_size >= 6 else "middel"
    else:
        destination = "BR3"
        score = (
            round(profile_score * 0.4 + float(war["score"]) * 0.6, 1)
            if profile_score is not None
            and isinstance(war.get("score"), (int, float))
            else None
        )
        score_basis = (
            "account_readiness_plus_own_war" if score is not None else "unavailable"
        )
        confidence = "hoog" if sample_size >= 6 else "middel"

    reasons = _destination_reason(
        destination,
        profile_score=profile_score,
        reliability=reliability,
        average_contribution=average_contribution,
        sample_size=sample_size,
        war_sufficient=war_sufficient,
    )
    if destination == "reject":
        reasons.append("Reliability ligt onder de expliciete reject-grens van 70%.")
    elif destination == "trial" and war_sufficient:
        reasons.append(
            "Account readiness: unknown; voldoende war-data vervangt ontbrekende "
            "profieldata niet, dus de bestemming blijft trial."
        )
    elif destination == "main":
        reasons.append(
            "Alle main-drempels zijn bekend en gehaald: readiness ≥75, "
            "reliability ≥95 en bijdrage ≥2600."
        )
    elif destination == "BR2":
        reasons.append(
            "BR2-drempels zijn bekend en gehaald: readiness ≥55, "
            "reliability ≥90 en bijdrage ≥2200."
        )
    elif destination == "BR3" and war_sufficient:
        reasons.append(
            "Eigen data is voldoende voor plaatsing, maar main/BR2-drempels "
            "zijn niet allemaal gehaald."
        )
    if score is None:
        reasons.append(
            "Aanbevelingsscore: unknown; een gecombineerd scoremodel vereist "
            "bekende profiel- én wardata."
        )
    else:
        reasons.append(f"Aanbevelingsscore: {score}/100 ({score_basis}).")

    return {
        "destination": destination,
        "recommended_destination": destination,
        "score": score,
        "score_max": 100,
        "score_basis": score_basis,
        "score_reason": reasons[-1],
        "confidence": confidence,
        "sample_size": sample_size,
        "sample_unit": "own_clan_races",
        "reason": " ".join(reasons),
        "reasons": reasons,
        "war_data_sufficient": war_sufficient,
        "leader_decision_type": T15_DESTINATION_TO_DECISION_TYPE[destination],
        "decision_rule": (
            f"Zonder minimaal {MIN_OBSERVED_WAR_RACES} complete eigen races is de "
            "bestemming altijd trial. Daarna: reliability <70% => reject; "
            "main vereist readiness ≥75, reliability ≥95 en bijdrage ≥2600; "
            "BR2 vereist readiness ≥55, reliability ≥90 en bijdrage ≥2200; "
            "overige voldoende geobserveerde profielen gaan naar BR3."
        ),
        "policy": {
            "minimum_observed_races": MIN_OBSERVED_WAR_RACES,
            "main_min_account_readiness": 75,
            "main_min_reliability": 95,
            "main_min_average_contribution": 2600,
            "br2_min_account_readiness": 55,
            "br2_min_reliability": 90,
            "br2_min_average_contribution": 2200,
            "reject_below_reliability": 70,
        },
    }


def classify_fit(
    profile: Dict[str, object],
    war: Dict[str, object],
    *,
    is_current_member: bool,
    recommendation: Optional[Dict[str, object]] = None,
) -> Dict[str, object]:
    """Retain the pre-T15 fit shape while exposing the new recommendation."""

    profile_score = profile.get("score")
    profile_score = (
        float(profile_score)
        if isinstance(profile_score, (int, float))
        else None
    )
    war_score = war.get("score")
    war_score = (
        float(war_score) if isinstance(war_score, (int, float)) else None
    )
    weeks_played = _safe_int(war.get("weeks_played"), minimum=0, maximum=52) or 0
    reliability = war.get("reliability")
    reliability = (
        float(reliability)
        if isinstance(reliability, (int, float))
        else None
    )
    average_contribution = war.get("average_contribution")
    average_contribution = (
        float(average_contribution)
        if isinstance(average_contribution, (int, float))
        else None
    )
    recommendation = recommendation or recommend_destination(profile, war)
    war_sufficient = bool(recommendation.get("war_data_sufficient"))

    if war_sufficient and profile_score is not None and war_score is not None:
        overall_score = round(profile_score * 0.4 + war_score * 0.6, 1)
    else:
        overall_score = profile_score

    reasons = list(profile.get("reasons") or [])
    reasons.extend(list(war.get("reasons") or []))
    if not reasons:
        reasons.append("Geen score-redenen beschikbaar; alle relevante data is unknown.")

    if not war_sufficient:
        label = (
            "Sterk profiel – proefperiode nodig"
            if profile_score is not None and profile_score >= 65
            else "Handmatige review – proefperiode nodig"
        )
        confidence = "laag"
    elif weeks_played < 4:
        confidence = "middel"
        if (
            reliability is not None
            and average_contribution is not None
            and reliability >= 90
            and average_contribution >= 2400
        ):
            label = "Veelbelovend in proefperiode"
        else:
            label = "Risico in proefperiode"
    else:
        confidence = "hoog" if weeks_played >= 6 else "middel"
        if (
            reliability is not None
            and average_contribution is not None
            and profile_score is not None
            and reliability >= 95
            and average_contribution >= 2600
            and profile_score >= 55
        ):
            label = "Sterke clan-fit"
        elif (
            reliability is not None
            and average_contribution is not None
            and reliability >= 90
            and average_contribution >= 2400
        ):
            label = "Goede clan-fit"
        elif reliability is not None and reliability >= 95:
            label = "Betrouwbaar, lagere war-output"
        elif reliability is not None and reliability >= 90:
            label = "Gemengde clan-fit"
        else:
            label = "Verhoogd war-risico"

    if not is_current_member and weeks_played:
        reasons.append(
            "Deze war-data komt uit eerder in onze eigen clan waargenomen races; "
            "externe profieldata is niet als warobservatie gebruikt."
        )
    reasons.append(
        f"Aanbeveling {recommendation.get('destination', 'trial')} is gebaseerd op "
        f"samplegrootte {recommendation.get('sample_size', 0)} own_clan_races."
    )

    return {
        "label": label,
        "confidence": confidence,
        "overall_score": overall_score,
        "score_basis": (
            "account_readiness_plus_own_war"
            if war_sufficient and profile_score is not None and war_score is not None
            else "account_readiness_only"
            if profile_score is not None
            else "unavailable"
        ),
        "score_reason": recommendation.get("score_reason"),
        "profile_weight_percent": 40 if war_sufficient else 100,
        "war_weight_percent": 60 if war_sufficient else 0,
        "sample_size": recommendation.get("sample_size", 0),
        "sample_unit": "own_clan_races",
        "reasons": reasons,
        "recommended_destination": recommendation.get("destination", "trial"),
        "recommendation": recommendation,
        "decision_rule": recommendation.get("decision_rule"),
    }


def build_fit_payload(
    player: Dict[str, object],
    war_rows: Iterable[Dict[str, object]],
    *,
    clan_tag: str,
) -> Dict[str, object]:
    """Build the complete, JSON-safe T15 screening read model."""

    source: Mapping[str, object] = player if isinstance(player, Mapping) else {}
    profile = build_profile_metrics(source)
    war = build_war_metrics(war_rows)
    current_clan_tag = profile.get("current_clan_tag")
    target_clan_tag = _safe_tag(clan_tag) or str(clan_tag or "").replace("#", "").upper()
    is_current_member = (
        isinstance(current_clan_tag, str)
        and current_clan_tag == target_clan_tag
    )
    recommendation = recommend_destination(profile, war)
    account_readiness = build_account_readiness(profile)
    observed_war_reliability = build_observed_war_reliability(war)
    trial_status = build_trial_status(war)
    fit = classify_fit(
        profile,
        war,
        is_current_member=is_current_member,
        recommendation=recommendation,
    )

    player_tag = _safe_tag(_value(source, "tag", "player_tag", "playerTag"))
    player_name = _safe_text(_value(source, "name", "player_name"))
    current_clan_name = profile.get("current_clan_name")
    mode = (
        "huidig_lid"
        if is_current_member
        else "eerder_geobserveerd"
        if war.get("observed_races", 0)
        else "extern"
    )
    manual_intake_checklist = [
        {
            "id": "war_availability",
            "label": "Bevestig dat de speler alle war-aanvallen op tijd kan en wil doen.",
            "required": True,
            "status": "open",
            "reason": "Niet betrouwbaar afleidbaar uit profiel- of historische API-data.",
        },
        {
            "id": "communication",
            "label": "Controleer taal, communicatie en praktische beschikbaarheid.",
            "required": True,
            "status": "open",
            "reason": "Niet aanwezig in de officiële player-API.",
        },
        {
            "id": "conduct",
            "label": "Controleer gedrag in clanchat/Discord volgens leidersafspraken.",
            "required": True,
            "status": "open",
            "reason": "Gedrag mag niet uit game-statistieken worden geconcludeerd.",
        },
        {
            "id": "trial_agreement",
            "label": "Leg de proefperiode en evaluatiemomenten expliciet vast.",
            "required": True,
            "status": "open" if trial_status["is_required"] else "recommended",
            "reason": trial_status["reason"],
        },
    ]
    manual_checks = [item["label"] for item in manual_intake_checklist]

    return {
        "player": {
            "tag": player_tag,
            "name": player_name,
            "clan_tag": current_clan_tag,
            "clan_name": current_clan_name,
            "current_clan": profile.get("current_clan"),
        },
        "mode": mode,
        "is_current_member": is_current_member,
        "profile": profile,
        "account_readiness": account_readiness,
        "war": war,
        "observed_war_reliability": observed_war_reliability,
        "trial_status": trial_status,
        "trial": trial_status,
        "recommendation": recommendation,
        "fit": fit,
        "manual_intake_checklist": manual_intake_checklist,
        # Kept for callers of the pre-T15 response shape.
        "manual_checks": manual_checks,
        "limitations": [
            "War-metrics gebruiken uitsluitend eigen, server-side opgeslagen clan-warobservaties voor deze clan en player tag.",
            "Externe profieldata en eventuele externe battlelogs worden nooit als eigen clan-warobservatie gebruikt.",
            "Minimaal twee complete eigen gespeelde races is een operationele proefperiodegrens, geen bewijs van levenslange betrouwbaarheid.",
            "Ontbrekende profiel- of warvelden zijn unknown en worden niet als nul meegerekend.",
            "Profiel readiness, observed war reliability en handmatige leidersbeoordeling blijven afzonderlijke signalen.",
        ],
        "screening_policy": {
            "minimum_observed_races": MIN_OBSERVED_WAR_RACES,
            "profile_signal_weights": dict(PROFILE_SIGNAL_WEIGHTS),
            "destinations": list(DESTINATIONS),
            "missing_value_policy": "unknown_not_zero",
            "war_observation_scope": "own_clan_history",
        },
        "data_sources": {
            "profile": "official_clash_api_normalized",
            "war": "own_clan_history_only",
        },
    }


__all__ = [
    "DEFAULT_WAR_WINDOW",
    "DESTINATIONS",
    "MIN_OBSERVED_WAR_RACES",
    "PROFILE_SIGNAL_NAMES",
    "PROFILE_SIGNAL_WEIGHTS",
    "T15_DESTINATION_TO_DECISION_TYPE",
    "build_account_readiness",
    "build_fit_payload",
    "build_observed_war_reliability",
    "build_profile_metrics",
    "build_trial_status",
    "build_war_metrics",
    "classify_fit",
    "normalized_card_level",
    "recommend_destination",
]
