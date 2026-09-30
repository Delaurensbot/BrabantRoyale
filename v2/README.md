# River Race V2

Open `/v2/` on this branch's Vercel Preview. `/` remains the original website.

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

`tests/verify_v2_browser.cjs` verifies the real browser flow, asset loading,
projection movement, selection, tied ranks, unavailable data, 320–1440px
layouts and reduced motion. Set `NODE_PATH` to the installed Playwright package
directory when using the bundled runtime. `V2_BROWSER_EXECUTABLE` can select an
installed Chromium binary. `V2_VERIFY_LIVE_API=1` additionally renders the live
production API payload through the local V2 fetch pipeline; it does not modify
production data. Screenshots are written outside the checkout by default.

The branch incorporates current main so the existing dashboard and short clan
chat summaries remain synchronized. The draft PR targets main. No production
deployment or merge is part of this experiment.

The environment and boats are baked 3D renders animated in the browser, not
downloadable 3D meshes. All website art is served locally as optimized WebP;
no third-party runtime, remote image service or render library is required.
Art prompts and saved assets are documented in `assets/ART_DIRECTION.md`.
