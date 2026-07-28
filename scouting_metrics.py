"""Transparent clan-fit metrics for Clash Royale player scouting."""

from __future__ import annotations

from typing import Dict, Iterable, List, Tuple


def clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


def normalized_card_level(card: Dict[str, object], global_max_level: int) -> int:
    """Convert rarity-relative API card levels to the displayed card level."""

    level = max(0, int(card.get("level") or 0))
    rarity_max = max(1, int(card.get("maxLevel") or global_max_level or 1))
    return max(0, level + max(0, global_max_level - rarity_max))


def build_profile_metrics(player: Dict[str, object]) -> Dict[str, object]:
    cards = list(player.get("cards") or [])
    global_max_level = max(
        [int(card.get("maxLevel") or 0) for card in cards] or [16]
    )
    display_levels = [
        normalized_card_level(card, global_max_level)
        for card in cards
    ]
    cards_level_16 = sum(1 for level in display_levels if level >= 16)
    cards_level_15_plus = sum(1 for level in display_levels if level >= 15)
    best_trophies = int(player.get("bestTrophies") or player.get("trophies") or 0)
    challenge_max_wins = int(player.get("challengeMaxWins") or 0)

    components = {
        "level_16_depth": round(clamp(cards_level_16 / 20) * 20, 2),
        "level_15_depth": round(clamp(cards_level_15_plus / 50) * 20, 2),
        "best_trophies": round(
            clamp((best_trophies - 6000) / 3000) * 15,
            2,
        ),
        "challenge_signal": round(clamp(challenge_max_wins / 12) * 5, 2),
    }
    profile_score = round(sum(components.values()) / 60 * 100, 1)

    return {
        "score": profile_score,
        "score_max": 100,
        "components": components,
        "cards_total": len(cards),
        "cards_level_15_plus": cards_level_15_plus,
        "cards_level_16": cards_level_16,
        "detected_max_card_level": global_max_level,
        "trophies": int(player.get("trophies") or 0),
        "best_trophies": best_trophies,
        "battle_count": int(player.get("battleCount") or 0),
        "wins": int(player.get("wins") or 0),
        "losses": int(player.get("losses") or 0),
        "challenge_max_wins": challenge_max_wins,
        "total_donations": int(player.get("totalDonations") or 0),
    }


def build_war_metrics(
    rows: Iterable[Dict[str, object]],
    *,
    window: int = 10,
) -> Dict[str, object]:
    ordered = sorted(
        list(rows),
        key=lambda row: str(row.get("race_created_at") or ""),
    )[-window:]
    played = [row for row in ordered if int(row.get("contribution") or 0) > 0]

    contributions = [int(row.get("contribution") or 0) for row in played]
    decks = [
        max(0, min(16, int(row.get("decks_used") or 0)))
        for row in played
    ]
    attacks_done = sum(decks)
    expected_attacks = len(played) * 16
    missed_attacks = max(0, expected_attacks - attacks_done)
    reliability = (
        round(attacks_done / expected_attacks * 100, 2)
        if expected_attacks
        else 0.0
    )
    average_contribution = (
        round(sum(contributions) / len(contributions), 2)
        if contributions
        else 0.0
    )
    perfect_weeks = sum(1 for decks_used in decks if decks_used == 16)
    perfect_rate = (
        round(perfect_weeks / len(played) * 100, 2)
        if played
        else 0.0
    )

    components = {
        "reliability": round(clamp((reliability - 75) / 25) * 22, 2),
        "contribution": round(
            clamp((average_contribution - 1800) / 1400) * 13,
            2,
        ),
        "perfect_week_rate": round(clamp(perfect_rate / 100) * 5, 2),
    }
    score = round(sum(components.values()) / 40 * 100, 1)

    return {
        "score": score,
        "score_max": 100,
        "components": components,
        "window_weeks": window,
        "weeks_observed": len(ordered),
        "weeks_played": len(played),
        "average_contribution": average_contribution,
        "attacks_done": attacks_done,
        "expected_attacks": expected_attacks,
        "missed_attacks": missed_attacks,
        "reliability": reliability,
        "perfect_weeks": perfect_weeks,
        "perfect_rate": perfect_rate,
        "first_observed_at": (
            str(ordered[0].get("race_created_at") or "") if ordered else None
        ),
        "last_observed_at": (
            str(ordered[-1].get("race_created_at") or "") if ordered else None
        ),
    }


def classify_fit(
    profile: Dict[str, object],
    war: Dict[str, object],
    *,
    is_current_member: bool,
) -> Dict[str, object]:
    profile_score = float(profile.get("score") or 0)
    war_score = float(war.get("score") or 0)
    weeks_played = int(war.get("weeks_played") or 0)
    reliability = float(war.get("reliability") or 0)
    average_contribution = float(war.get("average_contribution") or 0)

    if weeks_played >= 2:
        overall_score = round(profile_score * 0.4 + war_score * 0.6, 1)
    else:
        overall_score = round(profile_score, 1)

    reasons: List[str] = []
    if profile.get("cards_level_16", 0):
        reasons.append(
            f"{profile.get('cards_level_16')} kaarten op level 16"
        )
    if profile.get("cards_level_15_plus", 0):
        reasons.append(
            f"{profile.get('cards_level_15_plus')} kaarten op level 15+"
        )

    if weeks_played < 2:
        label = (
            "Sterk profiel – proefperiode nodig"
            if profile_score >= 65
            else "Handmatige review – proefperiode nodig"
        )
        confidence = "laag"
        reasons.append("Nog geen betrouwbare war-steekproef in onze historie")
    elif weeks_played < 4:
        confidence = "middel"
        if reliability >= 90 and average_contribution >= 2400:
            label = "Veelbelovend in proefperiode"
            reasons.append(
                f"{reliability}% reliability over {weeks_played} weken"
            )
        else:
            label = "Risico in proefperiode"
            reasons.append(
                f"{war.get('missed_attacks', 0)} gemiste aanvallen "
                f"over {weeks_played} weken"
            )
    else:
        confidence = "hoog" if weeks_played >= 6 else "middel"
        if (
            reliability >= 95
            and average_contribution >= 2600
            and profile_score >= 55
        ):
            label = "Sterke clan-fit"
        elif reliability >= 90 and average_contribution >= 2400:
            label = "Goede clan-fit"
        elif reliability >= 95:
            label = "Betrouwbaar, lagere war-output"
        elif reliability >= 90:
            label = "Gemengde clan-fit"
        else:
            label = "Verhoogd war-risico"
        reasons.extend(
            [
                f"{reliability}% reliability over {weeks_played} weken",
                f"Gemiddelde war-score {average_contribution}",
            ]
        )

    if not is_current_member and weeks_played:
        reasons.append("War-data is uit een eerdere periode in deze clan")

    return {
        "label": label,
        "confidence": confidence,
        "overall_score": overall_score,
        "profile_weight_percent": 40 if weeks_played >= 2 else 100,
        "war_weight_percent": 60 if weeks_played >= 2 else 0,
        "reasons": reasons,
        "decision_rule": (
            "Profieldata is een voorselectie. Vanaf 2 gespeelde war-weken "
            "weegt eigen clanobservatie voor 60% mee; bij minder data blijft "
            "een proefperiode verplicht."
        ),
    }


def build_fit_payload(
    player: Dict[str, object],
    war_rows: Iterable[Dict[str, object]],
    *,
    clan_tag: str,
) -> Dict[str, object]:
    profile = build_profile_metrics(player)
    war = build_war_metrics(war_rows)
    player_clan = player.get("clan") or {}
    current_clan_tag = str(player_clan.get("tag") or "").replace("#", "").upper()
    is_current_member = current_clan_tag == clan_tag.replace("#", "").upper()
    fit = classify_fit(profile, war, is_current_member=is_current_member)

    if is_current_member:
        mode = "huidig_lid"
    elif war.get("weeks_observed"):
        mode = "eerder_geobserveerd"
    else:
        mode = "extern"

    return {
        "player": {
            "tag": str(player.get("tag") or ""),
            "name": str(player.get("name") or ""),
            "clan_tag": str(player_clan.get("tag") or ""),
            "clan_name": str(player_clan.get("name") or ""),
        },
        "mode": mode,
        "profile": profile,
        "war": war,
        "fit": fit,
        "manual_checks": [
            "Kan en wil de speler alle war-aanvallen op tijd doen?",
            "Past taal, communicatie en beschikbaarheid bij de clan?",
            "Is gedrag in chat/Discord respectvol en betrouwbaar?",
            "Spreek een proefperiode van 2–4 war-weken af.",
        ],
        "limitations": [
            "De officiële player-API geeft geen volledige externe war-historie.",
            "Een recente battlelog bewijst geen langdurige war-betrouwbaarheid.",
            "Communicatie en gedrag kunnen niet verantwoord uit profielstats "
            "worden afgeleid.",
        ],
    }
