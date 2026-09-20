# River Race V2 experiment

Open `/v2/` on this branch's Vercel Preview. `/` remains the original website.

The illustrated river uses official `overview_rows` from the same
`/api/test-clan-prototype` request as the existing dashboard. Boat labels show
today's used/available attacks and the API's average points per deck. Live and
Projection compare day medals (cumulative medals during Colosseum), not actual
in-game boat distance. Both views use a shared visual scale. Missing scores
remain unavailable rather than becoming zero. Animations respect reduced motion.

All eleven existing dashboard cards, conditional cards, copy controls, clan
switches and links are retained below the river. Existing legacy/scraper-backed
cards continue using the existing `/api/cwstats` data; the river never uses it.
Backend routes and the original pages have not been changed.

This is a standalone HTML experiment copied from the current dashboard. Until
it is consolidated into shared components, functional changes to the original
dashboard should also be reflected here. Preservation tests guard existing IDs,
card counts, copy actions and helper functions.

## Checks

Run `python -m pytest -q` and `node --check v2/river.js`.
For local visual tests, serve the repository with `python -m http.server 8765`.
Static hosting has no API functions; the browser fixture in
`tests/v2_browser_fixture.js` supplies explicit synthetic data for tests only.
It is never loaded by the website. Evaluate it in a local browser to exercise
the real rendering pipeline, projection toggle, empty values and error states.

The V2 branch is based on `codex/shareable-war-summary` to retain the latest
shareable summaries. The draft PR targets that branch so only V2 changes are
shown. No production deployment or merge is part of this experiment.
