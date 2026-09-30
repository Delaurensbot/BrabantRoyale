# River Race V2

V2 is the homepage at `/`. `/v2/` remains an identical alias. The pre-rollout
homepage is preserved at `/classic/`, linked by **Origineel** in the navigation.
Both new entrypoints use the same `/v2/` scripts, styles and local art assets.

## Official war phases

The hero uses only `race_state` from `/api/test-clan-prototype` to choose its
phase. Explicit training/practice takes priority, even if a contradictory
Colosseum flag is present. `period_index` identifies the training day within
the official seven-period section; no computer-date or section-number guess
is used to declare a competition.

- River Race: river world, current-day points, current leader, ranked dock,
  and an explicitly estimated Projection view.
- Colosseum: dedicated desktop and mobile water-arena worlds. Scores are
  cumulative; average points per deck uses all played decks. Attacks today
  stay separate from cumulative attacks in the selected-clan panel.
- Training: prominent non-competitive banner, boats aligned equally, no
  ranks or competitive projections. Available API figures remain inspectable.
  During the final season week, the Colosseum arena and preparation information
  are already shown, with “Trainingsdag … voor Colosseum”. The additive
  `week_context` metadata is a local inference from official `sectionIndex` /
  `periodIndex` and the server UTC season calendar, not an official phase flag
  and never scraper data. It handles both four- and five-week seasons and
  stays unknown for missing, inconsistent or previous-week indices. Around
  a Monday API rollover, neutral training is safer than guessing a new season.
  It never changes medals, averages, rankings or the competitive Colosseum flag.
- Unknown/loading/error: no invented phase or ranking; stale clan and leader
  details are cleared. Missing scores remain unavailable, not zero.
  Only loading shows the quiet three-dot animation; it stops for success,
  errors and empty data, respects reduced motion and pauses offscreen.

Calendar rules are documented by Supercell: [Seasons](https://support.supercell.com/clash-royale/en/articles/seasons.html)
and [About Clan Wars](https://support.clashroyale.com/hc/en-us/articles/49484904148891-About-Clan-Wars).
The preparation theme is an inference using these rules; active competition
continues to follow the official API only.

The leader banner always identifies the current official leader, including
ties. Projection reorders the compact scoreboard by estimated score without
claiming that the forecast is an observed result. Original dashboard cards
below the hero keep their existing data and behaviour.

The 3D-rendered river world uses official `overview_rows` from the same
`/api/test-clan-prototype` request as the existing dashboard. Boat labels show
today's used/available attacks and the API's average points per deck. Live and
Projection compare day medals (cumulative medals during Colosseum), not actual
in-game boat distance. Both views use a shared visual scale. Missing scores
remain unavailable rather than becoming zero. Animations respect reduced motion
and pause when the page or scene is out of view. Blue boats represent our clan;
red boats represent opponents. Select a boat for exact clan figures. Equal scores
receive equal ranks. Mobile has its own portrait landscape and all five boats
remain visible, with a compact scoreboard beneath the river.

All eleven existing dashboard cards, conditional cards, copy controls, clan
switches and links are retained below the river. Existing legacy/scraper-backed
cards continue using the existing `/api/cwstats` data; the river never uses it.
The official endpoint adds only `week_context` display metadata; existing score
calculations and fields are unchanged. The previous homepage is retained exactly
at `/classic/`; analytics, joins and the test page remain at their existing URLs.

The root homepage and `/v2/` currently share identical HTML copies; changes must
keep both in sync until they are consolidated. Preservation tests guard that
identity, existing IDs, card counts, copy actions and helper functions against
the retained classic page, plus its exact normalized content at main c9c78a9.

## Open-clan warnings

An amber floating `!` marks a confirmed open clan from training day 3 onwards
and during River Race / Colosseum battle days. It follows the boat in Live and
Projection, and is repeated in the dock and selected-clan details. It does not
appear on training days 1–2, an unknown phase, invite-only / closed clans, or
missing/untrusted/inconsistent status. Animation respects reduced motion and
the existing page/offscreen pause controls.

The official endpoint adds `overview_rows[].clan_access` with `type`, tri-state
`is_open` (`true`, `false`, `null`) and `source` (`official_api`, `unknown`).
The already-fetched own-clan profile is reused. Only during the warning window,
up to four opponents from the official race are queried in parallel through
the same official API proxy, with a short 3-second request timeout each. These
optional lookups have no cache: page load / Refresh checks their current status.
Timeouts, invalid JSON, missing types and mismatched profile tags leave that
clan unknown, not closed, and do not fail the rest of the race response.
Training days 1–2 do not make additional opponent calls. Score fields, existing
cards, scraper-backed legacy cards and the classic snapshot are not modified.

## Checks

Run `python -m pytest -q` and `node --check v2/river.js`.
For local visual tests, serve the repository with `python -m http.server 8765`.
Static hosting has no API functions; the browser fixture in
`tests/v2_browser_fixture.js` supplies explicit synthetic data for tests only.
It is never loaded by the website. Evaluate it in a local browser to exercise
the real rendering pipeline, projection toggle, empty values and error states.

`tests/verify_v2_browser.cjs` verifies `/`, `/v2/`, `/classic/`, the real browser flow, asset loading,
projection movement, selection, tied ranks, unavailable data, 320–1440px
layouts and reduced motion. Set `NODE_PATH` to the installed Playwright package
directory when using the bundled runtime. `V2_BROWSER_EXECUTABLE` can select an
installed Chromium binary. `V2_VERIFY_LIVE_API=1` additionally renders the live
production API payload through the local V2 fetch pipeline; it does not modify
production data. Screenshots are written outside the checkout by default.

The branch incorporates current main so the existing dashboard and short clan
chat summaries remain synchronized. The user approved the homepage rollout on
30 September 2026. PR #164 carries that rollout through the existing GitHub to
Vercel deployment workflow.

The environment and boats are baked 3D renders animated in the browser, not
downloadable 3D meshes. All website art is served locally as optimized WebP;
no third-party runtime, remote image service or render library is required.
Art prompts and saved assets are documented in `assets/ART_DIRECTION.md`.
