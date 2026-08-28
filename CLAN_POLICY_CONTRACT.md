# T13 clan policy and leader decisions

T13 adds server-side storage for configurable clan rules and for the human
decisions that explain exceptions or role changes. It does not change the
T07 Duel-first classifier, the T08 monitor, analytics calculations, scouting,
or the existing frontend. Those consumers can read the policy through the
server module in a later task without hardcoded rule changes in this task.

## Storage model

Apply the migration in
`supabase/migrations/20260827210000_clan_policy_and_leader_decisions.sql`.

`public.clan_policy_settings` has one row per `clan_tag`. It is safe to read
publicly because it contains no player, actor, or reason data. The migration
seeds the three configured clans and gives future rows the same defaults.
Writes are restricted by the existing
`private.has_valid_analytics_admin_key()` RLS function.

`public.leader_decisions` is append-only for the API trust boundary. It stores
`clan_tag`, optional `player_tag`, `actor`, `decision_type`, `reason`, optional
`related_race_key`, `created_at`, and a scoped `idempotency_key`. There is no
public SELECT policy. A repeated request with the same
`clan_tag` + `idempotency_key` cannot create a second row.

## Explicit policy defaults and bounds

| Field | Default | Allowed value |
| --- | ---: | --- |
| `duel_first_enabled` | `false` | boolean |
| `duel_first_alert_after_utc` | `12:00:00Z` | UTC time `HH:MM:SSZ` (input also accepts `HH:MM`) |
| `promotion_window_weeks` | `6` | `2`, `4`, or `6` |
| `promotion_min_average` | `2500` | integer `0..10000` |
| `promotion_min_reliability` | `95.0` | number `0..100`, max two decimals |
| `promotion_min_observed_races` | `2` | integer `1..52` |
| `demotion_window_weeks` | `10` | integer `1..52` |
| `demotion_max_missed_attacks` | `2` | integer `0..832` |
| `trial_races_required` | `2` | integer `1..52` |

The Duel-first default is deliberately fail-closed: a missing policy row does
not enable a new alert path. Existing promotion/demotion/trial defaults match
the currently documented analytics/scouting rules. A missing policy column is
normalized to the corresponding default; an explicit invalid value is rejected
or treated as an unavailable stored policy, never exposed as an arbitrary
`null`.

`decision_type` must be one of:

`promotion`, `demotion`, `exemption`, `strategic_experiment`, `main_clan`,
`BR2`, `BR3`, `reject`, `manual_correction`.

`actor` is a bounded audit label (maximum 120 characters), not a new identity
system. `reason` is required and bounded to 240 characters. `related_race_key`
is optional and bounded to 256 characters. The existing admin key proves write
access; it does not prove the human identity written in `actor`.

## API response forms

### Public policy read

`GET /api/clan_policy?clan=<CLAN_TAG>` returns the safe policy model:

```json
{
  "ok": true,
  "status": "stored",
  "data_status": "stored",
  "clan_tag": "9YP8UY",
  "policy_source": "stored",
  "policy": {
    "clan_tag": "9YP8UY",
    "duel_first_enabled": false,
    "duel_first_alert_after_utc": "12:00:00Z",
    "promotion_window_weeks": 6,
    "promotion_min_average": 2500,
    "promotion_min_reliability": 95.0,
    "promotion_min_observed_races": 2,
    "demotion_window_weeks": 10,
    "demotion_max_missed_attacks": 2,
    "trial_races_required": 2
  }
}
```

The policy fields are also present at the top level for simple server callers;
the nested `policy` object is canonical. When Supabase is unavailable the
response has `ok: false`, `status: "error"`, `policy_source: "defaults"`, and
the same complete, safe default policy so consumers can fail closed without
inventing nulls. No leader decision data is joined into this route.

### Admin policy write

`POST`, `PUT`, or `PATCH /api/clan_policy` accepts a JSON object with
`clan_tag` and any policy fields. Omitted fields are filled with the explicit
defaults before the full row is upserted. Send the existing
`X-Analytics-Admin-Key` header. The response has the same public policy shape;
the key is never echoed.

### Admin decision write/read

`POST /api/leader_decisions` accepts the audit fields and the optional
`idempotency_key`. `created_at` is generated server-side when omitted. The
response is admin-only and contains the normalized `decision` plus its audit
fields.

`GET /api/leader_decisions?clan=<CLAN_TAG>&limit=50` returns at most 100
decisions for exactly that clan, newest first. Both endpoints require the
existing `X-Analytics-Admin-Key`; unauthenticated requests receive only
`{"ok":false,"error":"Unauthorized."}`. Public routes never return
`actor`, `reason`, or individual leader decisions.

## Assumptions and boundaries

- Clash tags are normalized to uppercase alphanumeric tags without `#`; the
  route does not silently map an unknown tag to the default clan.
- The admin key remains the existing database-checked key used by analytics
  week overrides. No full authentication or identity migration is part of
  T13.
- Policy values are intentionally readable as configuration. If the product
  later treats thresholds as confidential, the policy table/view and read
  route must be tightened together.
- `exemption` and `strategic_experiment` are stored as recognizable decision
  types for future analytics. T13 does not alter week-history inclusion or
  implement T14–T16 behavior.
- No production deployment, Supabase mutation, Discord call, or frontend change
  is performed by this repository task.
