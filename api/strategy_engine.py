from datetime import datetime, timedelta, timezone
import math
import re


WIN_SCORE = 200
LOSS_SCORE = 100
THEORETICAL_PLAYER_CAPACITY = 50
DECKS_PER_PLAYER = 4
THEORETICAL_DECK_CAPACITY = THEORETICAL_PLAYER_CAPACITY * DECKS_PER_PLAYER


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
    reset = next_deck_reset_utc(current)
    return {
        "mode": (war_phase or {}).get("mode") or "river_race",
        "warDay": (war_phase or {}).get("day"),
        "now": current.isoformat(),
        "nextDeckResetUtc": reset.isoformat(),
        "secondsUntilReset": max(0, int((reset - current.astimezone(timezone.utc)).total_seconds())),
        "raceFinished": False,
        "placementFrozenAfterFinish": False,
        "targetRank": target_rank,
        "riskProfile": risk_profile,
        "scoreRules": {
            "win": WIN_SCORE,
            "loss": LOSS_SCORE,
            "expectedBoat": None,
            "tieMargin": 1,
        },
    }


def normalize_clans(clans, clan_name, players=None, finish_outlook=None):
    current_sorted = sorted(
        clans or [],
        key=lambda c: (c.current_medals if c.current_medals is not None else -1, c.name.lower()),
        reverse=True,
    )
    projected_sorted = sorted(
        clans or [],
        key=lambda c: (c.projected_medals if c.projected_medals is not None else -1, c.name.lower()),
        reverse=True,
    )
    current_rank_by_name = {normalize_name(c.name): index for index, c in enumerate(current_sorted, start=1)}
    projected_rank_by_name = {normalize_name(c.name): index for index, c in enumerate(projected_sorted, start=1)}
    own_key = normalize_name(clan_name)
    players = players or []
    finish_outlook = finish_outlook or {}

    player_used = 0
    player_used_known = False
    for player in players:
        used = safe_int(player.get("decks_used_today"))
        if used is None:
            continue
        player_used += max(0, used)
        player_used_known = True

    finish_battles_left = safe_int(finish_outlook.get("battles_left"))

    rows = []
    for clan in current_sorted:
        key = normalize_name(clan.name)
        warnings = []
        decks_used = safe_int(clan.decks_used_today)
        decks_total = safe_int(clan.decks_total_today)
        confidence = "high"
        capacity_source = "live"

        if key == own_key and decks_used is None and player_used_known:
            decks_used = player_used
            confidence = "medium"
            capacity_source = "player_rows"
            warnings.append("Decks gebruikt geschat uit spelersrijen.")

        if key == own_key and finish_battles_left is not None:
            if decks_used is not None and decks_total is None:
                decks_total = decks_used + finish_battles_left
                confidence = "medium"
                capacity_source = "cwstats_battles_left"
                warnings.append("Deckcapaciteit geschat uit decks gebruikt + battles left.")
            elif decks_used is None:
                decks_total = None

        capacity_label = "live"
        if decks_total is None and decks_used is not None:
            decks_total = THEORETICAL_DECK_CAPACITY
            capacity_label = "theoretical"
            confidence = "low"
            capacity_source = "theoretical_50x4"
            warnings.append("Deckcapaciteit gebruikt theoretisch maximum 50 spelers x 4.")
        elif capacity_source != "live":
            capacity_label = "estimated"

        if decks_total is not None and decks_used is not None and decks_used > decks_total:
            warnings.append("Decks gebruikt was groter dan capaciteit; resterende decks op 0 gezet.")

        decks_remaining = None
        if decks_used is not None and decks_total is not None:
            decks_remaining = max(0, decks_total - decks_used)

        current_medals = safe_int(clan.current_medals)
        today_avg = safe_number(clan.avg_medals_per_deck)
        projected = safe_int(clan.projected_medals)
        historical_avg = today_avg
        if projected is not None and current_medals is not None and decks_remaining:
            historical_avg = max(0, (projected - current_medals) / decks_remaining)

        if today_avg is not None and historical_avg is not None and decks_used is not None:
            today_weight = min(0.75, decks_used / 100)
            blended = (today_weight * today_avg) + ((1 - today_weight) * historical_avg)
        else:
            blended = today_avg if today_avg is not None else historical_avg

        rows.append({
            "id": key,
            "name": clan.name,
            "currentRank": current_rank_by_name.get(key),
            "projectedRank": projected_rank_by_name.get(key),
            "currentMedals": current_medals,
            "decksUsedToday": decks_used,
            "estimatedDeckCapacityToday": decks_total,
            "decksRemainingToday": decks_remaining,
            "deckCapacityLabel": capacity_label,
            "deckCapacitySource": capacity_source,
            "participantsToday": None,
            "expectedParticipantsToday": THEORETICAL_PLAYER_CAPACITY if capacity_label == "theoretical" else None,
            "todayAveragePerDeck": today_avg,
            "historicalAveragePerDeck": historical_avg,
            "blendedAveragePerDeck": blended,
            "boatAttacksToday": None,
            "boatState": "unknown",
            "boatValueRaw": safe_int(clan.boat_points),
            "boatValueLabel": "Boat movement",
            "dataConfidence": confidence,
            "projected": projected,
            "trophies": safe_int(clan.trophies),
            "warnings": warnings,
        })

    return rows


def project_clan(clan, score_rules=None):
    score_rules = score_rules or {}
    win = safe_number(score_rules.get("win")) or WIN_SCORE
    loss = safe_number(score_rules.get("loss")) or LOSS_SCORE
    medals = safe_number(clan.get("currentMedals"))
    remaining = safe_int(clan.get("decksRemainingToday"))
    blended = safe_number(clan.get("blendedAveragePerDeck"))
    if medals is None or remaining is None or blended is None:
        return {
            "clanId": clan.get("id"),
            "floorFinal": None,
            "expectedFinal": None,
            "optimisticFinal": None,
            "ceilingFinal": None,
            "expectedRemainingDecks": remaining,
            "confidence": clan.get("dataConfidence", "low"),
        }

    optimistic_avg = max(blended, safe_number(clan.get("todayAveragePerDeck")) or blended)
    return {
        "clanId": clan.get("id"),
        "floorFinal": int(round(medals + remaining * loss)),
        "expectedFinal": int(round(medals + remaining * blended)),
        "optimisticFinal": int(round(medals + remaining * optimistic_avg)),
        "ceilingFinal": int(round(medals + remaining * win)),
        "expectedRemainingDecks": remaining,
        "p10Final": int(round(medals + remaining * loss)),
        "p50Final": int(round(medals + remaining * blended)),
        "p90Final": int(round(medals + remaining * min(win, optimistic_avg))),
        "confidence": clan.get("dataConfidence", "low"),
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
    best = rank_for_score(projections, our_clan.get("id"), own_projection.get("ceilingFinal"), "floorFinal")
    expected = rank_for_score(projections, our_clan.get("id"), own_projection.get("expectedFinal"), "expectedFinal")
    worst = rank_for_score(projections, our_clan.get("id"), own_projection.get("floorFinal"), "ceilingFinal")
    current_locked = best is not None and best == worst

    desired_locked = False
    if target_rank and own_projection.get("floorFinal") is not None:
        guaranteed_better = 0
        for projection in projections:
            if projection.get("clanId") == our_clan.get("id"):
                continue
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
    }


def build_rank_targets(our_clan, opponents, projections, score_rules=None):
    score_rules = score_rules or {}
    win = safe_int(score_rules.get("win")) or WIN_SCORE
    loss = safe_int(score_rules.get("loss")) or LOSS_SCORE
    tie_margin = safe_int(score_rules.get("tieMargin")) or 1
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
    target_scores = {}
    total_clans = len(opponents) + 1
    for rank in range(1, total_clans + 1):
        if rank == total_clans:
            target_scores[rank] = 0
        else:
            idx = rank - 1
            target_scores[rank] = scores[idx] if idx < len(scores) else 0

    targets = []
    for rank in range(1, total_clans + 1):
        target_score = safe_int(target_scores.get(rank))
        if target_score is None:
            continue
        needed_total = target_score + tie_margin
        required_additional = max(0, needed_total - current)
        required_avg = (required_additional / remaining) if remaining > 0 else None
        raw_wins = (needed_total - current - remaining * loss) / (win - loss) if remaining > 0 else 0
        wins = math.ceil(raw_wins)
        if required_avg is not None and required_avg > win:
            wins = None
            safe_losses = None
            status = "impossible"
        else:
            wins = max(0, min(remaining, wins))
            safe_losses = remaining - wins
            if wins == 0:
                status = "safe"
            elif required_avg is not None and required_avg <= (win * 0.85):
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
        })
    return targets


def build_score_plan(our_clan, target_score, score_rules=None):
    score_rules = score_rules or {}
    win = safe_int(score_rules.get("win")) or WIN_SCORE
    loss = safe_int(score_rules.get("loss")) or LOSS_SCORE
    tie_margin = safe_int(score_rules.get("tieMargin")) or 1
    current = safe_int(our_clan.get("currentMedals"))
    remaining = safe_int(our_clan.get("decksRemainingToday"))
    score = safe_int(target_score)
    if current is None or remaining is None or score is None or win <= loss:
        return None

    needed_total = score + tie_margin
    required_additional = max(0, needed_total - current)
    required_avg = (required_additional / remaining) if remaining > 0 else None
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
        }

    wins = max(0, min(remaining, wins))
    return {
        "targetScore": score,
        "requiredAdditionalMedals": required_additional,
        "requiredAveragePerDeck": round(required_avg, 2) if required_avg is not None else None,
        "minimumWinsNeeded": wins,
        "safeLossesAllowed": remaining - wins,
        "status": "safe" if wins == 0 else "stretch",
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


def evaluate_boat_strike(context, our_clan, target_clan, normal_battle_expected_score, boat_battle_expected_score, estimated_attacks_to_disable, estimated_opponent_score_lost, safety_buffer=100):
    day = context.get("warDay")
    direct_value = estimated_opponent_score_lost
    if day == 4 and not context.get("raceFinished"):
        direct_value = estimated_opponent_score_lost
    if day == 4 and estimated_opponent_score_lost in (None, 0):
        return {
            "recommend": False,
            "targetClanId": target_clan.get("id") if target_clan else None,
            "attacksNeeded": estimated_attacks_to_disable,
            "maximumSafeBoatAttacks": 0,
            "completionProbability": None,
            "medalOpportunityCost": None,
            "estimatedOpponentDelayValue": estimated_opponent_score_lost,
            "netStrategicValue": None,
            "reason": "Dag 4 boat attacks hebben alleen waarde met direct effect voor reset.",
        }
    if estimated_attacks_to_disable is None or estimated_attacks_to_disable <= 0:
        return {
            "recommend": False,
            "targetClanId": target_clan.get("id") if target_clan else None,
            "attacksNeeded": estimated_attacks_to_disable,
            "maximumSafeBoatAttacks": 0,
            "completionProbability": None,
            "medalOpportunityCost": None,
            "estimatedOpponentDelayValue": estimated_opponent_score_lost,
            "netStrategicValue": None,
            "reason": "Benodigde boat attacks onbekend.",
        }
    opportunity = estimated_attacks_to_disable * max(0, normal_battle_expected_score - boat_battle_expected_score)
    net = (direct_value or 0) - opportunity
    remaining = safe_int(our_clan.get("decksRemainingToday")) or 0
    max_safe = min(remaining, estimated_attacks_to_disable)
    recommend = net > safety_buffer and max_safe >= estimated_attacks_to_disable
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
    }


def recommend_strategy(context, our_clan, opponents, projections, rank_targets):
    warnings = []
    assumptions = [
        "Tie-break gebruikt conservatief +1 punt omdat exacte tie-break niet beschikbaar is.",
        "Matchmakingeffect van verliezen is communityhypothese en niet exact gepubliceerd door Supercell.",
    ]
    missing = []
    for field in ("currentMedals", "decksRemainingToday"):
        if our_clan.get(field) is None:
            missing.append(field)
    if not context.get("scoreRules"):
        missing.append("scoreRules")
    if missing:
        return {
            "mode": "DATA_INCOMPLETE",
            "headline": "Onvoldoende betrouwbare data voor strategieadvies",
            "summary": "Essentiele velden ontbreken: " + ", ".join(missing),
            "confidence": "low",
            "classification": "data-derived",
            "targetClanId": None,
            "targetRank": None,
            "actionPlan": {
                "minimumWins": 0,
                "maximumSafeLosses": 0,
                "recommendedBoatAttacks": 0,
                "decksToHoldTemporarily": 0,
            },
            "requiredAveragePerDeck": None,
            "bestPossibleRank": None,
            "expectedRank": None,
            "worstPossibleRank": None,
            "rationale": ["Geen harde aanbeveling omdat data ontbreekt."],
            "warnings": warnings,
            "assumptions": assumptions,
        }

    bounds = compute_rank_bounds(our_clan, projections, context.get("targetRank", 1), context.get("scoreRules"))
    if not bounds:
        return {
            "mode": "DATA_INCOMPLETE",
            "headline": "Onvoldoende betrouwbare projectiedata",
            "summary": "Kan rangscenario's niet berekenen.",
            "confidence": "low",
            "classification": "data-derived",
            "targetClanId": None,
            "targetRank": None,
            "actionPlan": {"minimumWins": 0, "maximumSafeLosses": 0, "recommendedBoatAttacks": 0, "decksToHoldTemporarily": 0},
            "requiredAveragePerDeck": None,
            "bestPossibleRank": None,
            "expectedRank": None,
            "worstPossibleRank": None,
            "rationale": ["Projectie ontbreekt voor onze clan."],
            "warnings": warnings,
            "assumptions": assumptions,
        }

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

    if context.get("mode") == "colosseum":
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
    elif target and target.get("minimumWinsNeeded") == 0:
        mode = "SAFE_LOSS_WINDOW"
        headline = f"Plaats {target_rank} is veilig haalbaar"
        summary = f"Er zijn {target.get('safeLossesAllowed')} veilige losses binnen dit doel."
    elif target and target.get("safeLossesAllowed") is not None and target.get("safeLossesAllowed") <= 5:
        mode = "ALL_OUT"
        headline = f"All-out voor plaats {target_rank}"
        summary = "Er is weinig veilige lossruimte; directe medailles hebben prioriteit."

    if mode in {"POSITION_LOCKED_THROW", "COLOSSEUM_LOCKED_THROW", "EARLY_FINISH_BALANCING"}:
        minimum_wins = 0
        safe_losses = safe_int(our_clan.get("decksRemainingToday")) or 0
        required_avg = None
        target_rank_output = bounds.get("expectedRank")
    elif mode == "PROTECT_CURRENT_POSITION" and protect_plan:
        minimum_wins = protect_plan.get("minimumWinsNeeded") or 0
        safe_losses = protect_plan.get("safeLossesAllowed") or 0
        required_avg = protect_plan.get("requiredAveragePerDeck")
        target_rank_output = current_rank
    elif target_for_plan:
        minimum_wins = target_for_plan.get("minimumWinsNeeded") or 0
        safe_losses = target_for_plan.get("safeLossesAllowed") or 0
        required_avg = target_for_plan.get("requiredAveragePerDeck")
        target_rank_output = target_for_plan.get("rank")
    else:
        minimum_wins = 0
        safe_losses = 0
        required_avg = None
        target_rank_output = None

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

    return {
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
        "rationale": rationale[:3],
        "warnings": warnings,
        "assumptions": assumptions,
    }


def build_strategy_package(clans, clan_name, players, finish_outlook, war_phase, now=None):
    normalized = normalize_clans(clans, clan_name, players=players, finish_outlook=finish_outlook)
    context = build_war_context(war_phase, now=now)
    projections = build_projections(normalized, context.get("scoreRules"))
    own = next((clan for clan in normalized if normalize_name(clan.get("name")) == normalize_name(clan_name)), None)
    if not own:
        recommendation = recommend_strategy(context, {"id": None}, [], projections, [])
        return {
            "warContext": context,
            "raceRows": normalized,
            "projections": projections,
            "rankTargets": [],
            "recommendation": recommendation,
            "dataQuality": {
                "confidence": "low",
                "missingFields": ["ourClan"],
                "estimatedFields": [],
                "sources": ["royaleapi", "cwstats"],
            },
        }
    opponents = [clan for clan in normalized if clan.get("id") != own.get("id")]
    targets = build_rank_targets(own, opponents, projections, context.get("scoreRules"))
    recommendation = recommend_strategy(context, own, opponents, projections, targets)
    missing = []
    estimated = []
    for field in ("currentMedals", "decksRemainingToday", "blendedAveragePerDeck"):
        if own.get(field) is None:
            missing.append(field)
    if own.get("deckCapacityLabel") != "live":
        estimated.append("deckCapacity")
    return {
        "warContext": context,
        "raceRows": normalized,
        "projections": projections,
        "rankTargets": targets,
        "recommendation": recommendation,
        "dataQuality": {
            "confidence": own.get("dataConfidence", "low"),
            "missingFields": missing,
            "estimatedFields": estimated,
            "sources": ["royaleapi", "cwstats"],
        },
    }
