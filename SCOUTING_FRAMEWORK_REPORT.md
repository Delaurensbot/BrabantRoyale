# Clan War Scouting Framework — Developer Report

## A. Feasibility analysis

### Data source baseline (official API via RoyaleAPI proxy)
Current server analytics already uses official Clash Royale API routes through `https://proxy.royaleapi.dev/v1` with server-side API key, specifically:
- `/clans/{tag}/members`
- `/clans/{tag}/riverracelog`
- `/clans/{tag}/currentriverrace`

This is visible in the existing analytics collector and is the correct baseline for scouting expansion.

---

### A) Player strength

#### Directly retrievable (external or internal player, by player endpoint)
From player profile endpoints (e.g. `/players/{tag}`), you can reliably retrieve:
- player tag
- player name
- experience / king level (the API exposes level fields)
- current trophies
- best trophies
- cards collection with per-card level/evolution metadata

This means card progression features are feasible **if we fetch player profile data per candidate**.

#### Derivable
From the card list in player profile:
- `level15_count` = number of cards at level 15
- `level16_count` (elite/max tier) = number of cards at elite/max level
- broader depth buckets (e.g. cards >= playable threshold)

#### Not reliably knowable from current stored war tables alone
If we only use existing weekly analytics tables (members + race + race log), then exact card depth (L15/L16 counts) is not available unless we add player-profile snapshots.

---

### B) Clan War history

#### Directly retrievable from clan war endpoints
For **our clan races** (`riverracelog` + `currentriverrace`), per participant in each race snapshot we can retrieve:
- fame
- repair points
- decks used
- participant tag/name

#### Derivable
For players appearing in our clan race history:
- observed war weekends played
- observed contribution per weekend (`fame + repairPoints`)
- observed decks used per weekend
- missed attacks per weekend (`16 - decksUsed`, clamped)

#### Availability scope
- **Internal / past members in our race snapshots:** yes, historical metrics are calculable for observed period.
- **External candidates not in our clan history:** no historical participation in *our* races; only profile strength can be fetched unless you also track their clan history elsewhere over time.

---

### C) Consistency and quality

#### Directly retrievable
Not as pre-aggregated values. API provides per-race participant rows; consistency metrics must be computed.

#### Derivable historically (for observed players in our clan history)
- average war contribution
- count of weekends above 3000 contribution
- % weekends above thresholds (2500/3000)
- total decks used
- average decks used per played weekend
- missed attacks total
- reliability %
- longest streak of perfect weekends (`decksUsed == 16`)
- current perfect streak
- incomplete weekend count
- zero-contribution weekend count

#### Caveat
These are only valid over the **observed window** (currently limited by available race log/current race returned and whatever we persist). They are not full-lifetime clan war history.

---

### D) API limitations

#### What we can calculate now for current/past members in our clan history
- Strong war behavior metrics for the observed race-log window, using participant rows from our clan races.
- Reliability/streak metrics based on decks used and contribution.

#### What we can calculate now for external candidates
- Profile strength/card progression (via player endpoint) if candidate tag is known.
- Very limited/no reliable long-term war consistency unless they are present in our stored history.

#### What requires future tracking/snapshots
To strengthen recruitment quality for externals and longer history coverage:
- periodic snapshots of candidate profiles (L15/L16 growth, trophies trend)
- periodic snapshots of candidate war participation data while they are in observed clans
- normalized historical storage keyed by `player_tag + war_week_key`

#### What is impossible/unreliable from official API alone (without prior storage)
- true lifetime count of war weekends played
- true lifetime longest perfect 16-deck streak
- complete historical missed-attacks totals outside observed data
- retrospective performance for a player never previously tracked in your dataset

---

## B. Proposed scouting framework

## 1) Profile Strength metrics
- `player_tag`
- `player_name`
- `exp_level`
- `trophies`
- `best_trophies`
- `cards_level_15`
- `cards_level_16`
- optional depth buckets:
  - `cards_ge_14`
  - `cards_ge_15`
  - `cards_ge_16`

## 2) War Experience metrics (observed scope)
Let each observed weekend `w` have:
- `contribution_w = fame_w + repair_w`
- `decks_used_w` (0..16)

Definitions:
- `total_wars_observed = count(all observed weekends in window)`
- `wars_played = count(w where contribution_w > 0)`
- `total_contribution = Σ contribution_w`
- `avg_contribution_played = total_contribution / wars_played`
- `wars_above_3000 = count(w where contribution_w >= 3000)`
- `pct_above_2500 = count(contribution_w >= 2500) / wars_played * 100`
- `pct_above_3000 = wars_above_3000 / wars_played * 100`
- `total_decks_used = Σ decks_used_w`
- `avg_decks_used_played = total_decks_used / wars_played`
- `missed_attacks_total = Σ max(0, 16 - decks_used_w)`

## 3) Consistency / Reliability
- `expected_attacks = wars_played * 16`
- `reliability_pct = total_decks_used / expected_attacks * 100`
- perfect war weekend rule: `decks_used_w == 16`
- `longest_perfect_streak = longest consecutive run of perfect weekends`
- `current_perfect_streak = perfect run from most recent observed weekend backward`
- `incomplete_weekends = count(w where 0 < decks_used_w < 16)`
- `zero_contribution_weekends = count(w where contribution_w == 0)`

Priority metrics requested by you are explicitly included:
- longest 16/16 streak
- average contribution
- count above 3000
- number of level 15 cards
- number of level 16 cards

## 4) Recruitment classification logic

### ELITE WAR TARGET
Recommended when all/most hold:
- strong profile strength (high king/exp and strong L15/L16 depth)
- `wars_played >= 8` (observed)
- `reliability_pct >= 95`
- `longest_perfect_streak >= 4`
- `avg_contribution_played >= 3000`
- `pct_above_3000 >= 60`

### STRONG TARGET
- reasonable profile strength
- `wars_played >= 6`
- `reliability_pct >= 88`
- `avg_contribution_played >= 2600`
- `pct_above_2500 >= 60`

### DEVELOPMENT TARGET
- adequate account/card baseline but limited consistency or sample size
- `wars_played >= 3`
- `reliability_pct >= 70`
- improving trend or decent profile strength

### RISKY / INCONSISTENT
- repeated missed attacks and/or low contribution:
- `reliability_pct < 70` or frequent incomplete/zero weeks

### UNKNOWN / NOT ENOUGH DATA
- insufficient observed weekends (e.g. `< 3`) or external candidate without historical war data

---

## 5) Two operating modes

### Mode A — Internal / observed players
Use full model:
- profile strength + full observed war analytics
- confidence: **high** (within observed window)

### Mode B — External candidates
Use reduced model:
- profile strength only (player endpoint)
- optional short trial signal once they join (first 2–4 weekends)
- confidence: **low-to-medium** until tracked war data accumulates

Risk mitigation for Mode B:
- provisional label capped at `DEVELOPMENT TARGET` or `UNKNOWN` until war sample threshold reached
- auto-upgrade classification after enough observed weekends

---

## C. Data model (normalized JSON for one player)

```json
{
  "player_tag": "ABC123",
  "player_name": "PlayerName",
  "mode": "internal",
  "confidence": "high",
  "profile_strength": {
    "exp_level": 63,
    "trophies": 9000,
    "best_trophies": 9100,
    "cards_level_15": 62,
    "cards_level_16": 18,
    "cards_ge_14": 95
  },
  "war_experience": {
    "window": {
      "weeks_observed": 10,
      "first_week_key": "74-1",
      "last_week_key": "76-2"
    },
    "total_wars_observed": 10,
    "wars_played": 9,
    "total_contribution": 27850,
    "avg_contribution_played": 3094.44,
    "wars_above_3000": 6,
    "pct_above_2500": 88.89,
    "pct_above_3000": 66.67,
    "total_decks_used": 140,
    "avg_decks_used_played": 15.56,
    "missed_attacks_total": 4
  },
  "consistency": {
    "expected_attacks": 144,
    "reliability_pct": 97.22,
    "longest_perfect_streak": 5,
    "current_perfect_streak": 3,
    "incomplete_weekends": 2,
    "zero_contribution_weekends": 1
  },
  "classification": {
    "label": "ELITE WAR TARGET",
    "reasons": [
      "Reliability >= 95%",
      "Avg contribution >= 3000",
      "Strong perfect streak"
    ]
  }
}
```

---

## D. Build plan

## Proposed API routes
1. `GET /api/scouting/player?tag=<playerTag>&mode=external|internal&clan=<clanTag>`
   - returns one normalized scouting payload.
2. `GET /api/scouting/recent-joins?clan=<clanTag>&limit=10`
   - wraps joins list + scouting-lite profile summary for last joins.
3. `GET /api/scouting/internal-summary?clan=<clanTag>&window=10`
   - bulk internal leaderboard for recruitment decisions.

## Proposed transformation/calculation files
- `scouting_model.py`
  - dataclasses / schema mapping and payload normalization.
- `scouting_metrics.py`
  - pure metric formulas (reliability, streaks, thresholds, percentages).
- `scouting_service.py`
  - orchestration: fetch API data, merge profile + war history, produce final classification.

Optional storage extensions:
- `data/scouting_snapshots/` JSON or DB-backed tables:
  - `player_profile_snapshots`
  - `war_week_player_stats`

## What to precompute server-side
- streaks and reliability math
- classification label + reason flags
- threshold counters (%>2500, %>3000, missed totals)
- confidence level based on mode/sample size

## What to render client-side
- already computed metrics and labels
- explanatory badges/reasons
- lightweight filters/sorting (label, reliability, L15/L16, avg contribution)

## Safest order of implementation
1. Add pure calculation layer (`scouting_metrics.py`) with deterministic unit tests.
2. Add normalized model/transform layer (`scouting_model.py`).
3. Add single-player API route (`/api/scouting/player`) and validate against both modes.
4. Add optional scouting test page (`scouting.html`) without touching landing page.
5. Expand to bulk/internal-summary route.
6. Add snapshot persistence jobs to improve external confidence over time.

---

## Requested joins-page adjustment
The recent joins page should only show the latest 10 players who joined your clan. This is now enforced by requesting `limit=10` in the API call and applying an additional client-side cap to 10 rows.
