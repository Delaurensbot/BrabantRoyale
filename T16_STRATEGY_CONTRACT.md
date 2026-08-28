# T16 strategy contract

T16 adds a server-side, read-only strategy report. It never performs a Clash
Royale game action. Existing `strategy`, `rank_targets`, and projection fields
remain available for compatibility; the additive fields are `strategy_report`,
`boat_eligibility` (also exposed as `boot_eligibility`), and `strategic_week`.

## Boat eligibility

Eligibility is `eligible`, `not_eligible`, or `unknown`. A candidate can only
be eligible when role, card depth, observed war reliability plus its sample,
current boat need, and a usable defense remainder are known. The default
thresholds are card depth `8`, observed reliability `90%`, and `2` observed
races; the strategy policy can override them. Missing defense fields stay
`null`/`unknown`; an observed zero is preserved as zero and is not treated as
missing data.

The report includes `advice_only: true`, `automatic_action: false`, and
`action: null`. Training, finished, stale/error, unavailable, and Colosseum
phases do not produce a boat recommendation; Colosseum is explicitly
`not_applicable`.

## Strategy modes and strategic weeks

The accepted modes are `normal`, `protect_position`, and
`strategic_experiment`. The legacy uppercase recommendation mode remains
unchanged; the selected T16 mode is available as `strategy_mode`.

`strategic_experiment` produces a `strategic_week` label containing `reason`,
`actor`, `race_key`, and `included_in_normal_analytics`. A complete label is
excluded from normal analytics by default, unless it explicitly opts in.
Incomplete metadata fails closed and remains included. Experiment output
reports only observed outcomes and uncertainties; it does not claim that
loose-to-win is guaranteed. T13 leader-decision and policy shapes are accepted
as input, while the existing admin/auth boundary is unchanged.
