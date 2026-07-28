# Analytics tables (official Clash Royale API version)

This page now uses the **official Clash Royale API** (`proxy.royaleapi.dev/v1`) with server-side `CLASH_ROYALE_API_KEY`.

## What we can retrieve directly

- Clan members + roles: `GET /clans/%23{tag}/members`
- Historic river races: `GET /clans/%23{tag}/riverracelog`
- Current river race (if active/available): `GET /clans/%23{tag}/currentriverrace`

For each participant per race we use:
- `fame`
- `repairPoints`
- `decksUsed`
- `tag`
- `name`

## Derived weekly model

The official API does not expose the same RoyaleAPI analytics table format. We therefore build our own weekly dataset:

- `week_key = "{seasonId}-{logical_week_index}"`
- `contribution = fame + repairPoints`
- `decks_used = clamp(decksUsed, 0..16)`
- include only players that are in current clan members list
- combine live race snapshots with the full stored Supabase history
- dedupe overlapping snapshots on `(seasonId, createdDate)` before numbering weeks
- assign week numbers sequentially per season based on chronological `createdDate`

## Table logic

## 1) Huidig seizoen MVP (Top 10)
- Current season = highest `seasonId` in available race snapshots.
- For each player, only races in that season.
- Played weekend if `Contribution > 0`.
- If played, must have `Decks Used = 16`.
- Score = sum of contribution over played races.
- Top 10 descending by score.

## 2) Vorig seizoen MVP (Top 10)
- Previous season = second-highest `seasonId`.
- Player must have, for every race in that season:
  - `Contribution > 0`
  - `Decks Used = 16`
- Score = season contribution sum.
- Top 10 descending by score.

## 3) Promotie naar Elder
- Role must be `member`.
- The clan leader selects a 2, 4, or 6 week evaluation window.
- Every included race in that window must be perfect (`D=16`).
- Average contribution inside the selected window must be >= 2500.
- Excluded player-weeks are removed from the window and do not break a streak.
- Sorted by streak desc, then avg contribution desc.

## 4) Degradatie naar Member
- Current role must be `elder`.
- Use at most the latest 10 included played races.
- Recommend demotion when missed attacks total is greater than 2.
- Show observed weeks, missed weekends, total missed attacks, and the reason.

## 5) Player-week exceptions
- Stored in Supabase by `(clan_tag, race_created_at, player_tag)`.
- The raw score and decks remain visible in player detail.
- An excluded week is removed from MVP, reliability, promotion, demotion, table
  totals, and scouting calculations.
- Writes require a table-specific analytics admin key. The browser never
  receives a Supabase secret/server key.

## 6) Reliability / Ratio Score
For each player over all available races:
- Played race if `Contribution > 0`
- Skip manually excluded player-weeks
- Expected attacks = 16 per played race
- `missing = 16 - decks_used`
- `attacks_done += decks_used`
- `missed_attacks += missing`
- `avg_points = total_contribution / weeks_played`
- `reliability = attacks_done / (weeks_played * 16) * 100`

Penalty points:
- miss 0 => +0
- miss 1 => +2
- miss 2 => +4
- miss 3 => +12
- miss >=4 => `missing * 4`

## 7) Underperformers
From ratio table:
- `avg_points < 2400`
- `reliability < 95`
- meaningful stats required
- `URGENT` badge if `missed_attacks >= 8` or `penalty_points >= 24`

## 8) Watchlist A
- `avg_points >= 2800`
- `reliability < 95`
- meaningful stats required

## 9) Watchlist B/C
Watchlist B:
- `weeks_played >= 5`
- `reliability >= 95`
- `avg_points < 2400`
- exclude protected player: `weeks_played >= 10 && missed_attacks == 0 && avg_points >= 2200`

Watchlist C (NEW):
- `weeks_played < 5`
- and (`reliability < 95` or `avg_points < 2400`)

## 10) Overperformers
- `avg_points >= 2800`
- `reliability >= 95`
- `missed_attacks <= 2`
- `weeks_played >= 5`
- sort: avg desc, reliability desc, missed asc

## 11) Contribution table
- Generated raw table from official API snapshots.
- Columns: `Player, Role, C, <week_keys...>`
- Cell value per week = `fame + repairPoints`.

## 12) Decks Used table
- Generated raw table from official API snapshots.
- Columns: `Player, Role, D, <week_keys...>`
- Cell value per week = clamped `decksUsed`.

## 13) New-player clan-fit screening
- Profile pre-screen from the official player endpoint:
  - level 15+ and level 16 card depth
  - current/best trophies
  - challenge max wins
- Own observed clan-war data, when available:
  - played weeks (latest 10)
  - average contribution
  - reliability and missed attacks
  - perfect-week rate
- With fewer than 2 played war-weeks, profile data has 100% of the displayed
  score and the recommendation always requires a trial period.
- From 2 played weeks onward, profile readiness has 40% weight and own war
  observations have 60% weight.
- Communication, availability, language, and conduct remain manual checks.

## Known limitation
- The official endpoints do not provide the same RoyaleAPI war/analytics HTML history layout.
- We therefore derive stable week columns from chronologically ordered race snapshots per season, instead of trusting the raw API `sectionIndex`.
- The first Supabase snapshot can only backfill races still returned by the Clash API. Older races that have already disappeared cannot be reconstructed; retention grows from the first successful scheduled snapshot onward.
- Incomplete live payloads without a real positive `seasonId` and valid
  `createdDate` are ignored. This prevents artificial rows such as `0-1`.
