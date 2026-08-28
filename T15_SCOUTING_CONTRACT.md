# T15 scouting contract

T15 screens a new player with two separate evidence tracks: account readiness
from the official player profile, and observed war reliability from the
player's own stored clan races. A profile never becomes a war observation.
Rows without both the requested player identity and the requested clan
identity are excluded before metrics are calculated.

## Public screening read model

`GET /api/scouting?tag=<PLAYER_TAG>&clan=<CLAN_TAG>` returns the allow-listed
screening payload used by `analytics.html`. It contains these separate sections:

- `account_readiness`: trophies, best trophies, detected card level, L15/L16
  depth, deck breadth, a normalized score, reasons, and profile sample size.
- `observed_war_reliability`: sample size in `own_clan_races`, reliability,
  missed attacks, average contribution, score, reasons, and uncertainty.
- `trial_status`: whether the two complete own-clan race observations needed
  for a permanent destination are still missing.
- `recommendation`: one exact destination: `main`, `BR2`, `BR3`, `trial`, or
  `reject`, always accompanied by a reason and sample size.
- `manual_intake_checklist`: leader checks that cannot be inferred safely from
  API data, including communication, conduct, and trial agreement.

Missing values are represented as `null`/`unknown`; they are not converted to
zero. Fewer than two complete own-clan race observations keeps the player on
`trial` and does not produce a full war score. A known zero contribution or
zero decks observation remains an observed race, but incomplete metrics keep
the reliability and recommendation evidence explicitly uncertain.

## Leader decision audit

The manual form in `analytics.html` posts only after the leader supplies the
existing `X-Analytics-Admin-Key` through the established session prompt. It
calls `POST /api/leader_decisions`, maps destinations to the existing T13
decision types (`main_clan`, `BR2`, `BR3`, `strategic_experiment`, `reject`),
and prefixes the stored reason with the exact T15 destination. The server-side
T13 route remains the authorization and storage boundary; public scouting
responses never include decision rows or the configured key. The configured
key is not embedded in the page source; it is supplied at runtime by the
leader through the existing browser-session pattern. No API, Supabase, or
other secret is embedded in `analytics.html`.

T15 does not select a boot strategy, schedule jobs, deploy production code, or
replace the existing analytics policy. It only provides evidence for a manual
leader intake decision.
