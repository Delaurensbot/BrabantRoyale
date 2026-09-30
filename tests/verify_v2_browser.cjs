/* Run against a local server. Requires Playwright via NODE_PATH or npm. */
const { chromium } = require("playwright");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const assert = require("node:assert/strict");

(async () => {
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.V2_BROWSER_EXECUTABLE || undefined,
  });
  const page = await browser.newPage({
    viewport: { width: 1440, height: 1100 },
  });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/**", (route) =>
    route.fulfill({ json: { ok: false, error: "Local fixture required" } }),
  );
  await page.goto(process.env.V2_TEST_URL || "http://127.0.0.1:8765/v2/");
  await page.waitForFunction(() => typeof window.RiverV2 === "object");
  const fixture = fs.readFileSync(
    path.join(__dirname, "v2_browser_fixture.js"),
    "utf8",
  );
  console.log(await page.evaluate(fixture));
  await page.evaluate(() =>
    Promise.all(
      Array.from(document.querySelectorAll("#riverHero img")).map((image) =>
        image.decode(),
      ),
    ),
  );
  await page.waitForTimeout(1800);
  const output =
    process.env.V2_SCREENSHOT_DIR ||
    path.join(os.tmpdir(), "brabant-royale-v2-review");
  fs.mkdirSync(output, { recursive: true });
  await page.screenshot({
    path: path.join(output, "desktop-live.png"),
    fullPage: false,
  });
  const live = await page.locator(".rv-vessel").first().boundingBox();
  await page.locator('[data-mode="projection"]').click();
  await page.waitForTimeout(1800);
  const projected = await page.locator(".rv-vessel").first().boundingBox();
  assert.ok(live && projected, "Boat geometry must be measurable");
  assert.ok(
    projected.y < live.y - 30,
    "Projection must visibly move the boat upriver",
  );
  await page.screenshot({ path: path.join(output, "desktop-projection.png") });
  await page.locator('[data-mode="live"]').click();
  for (const width of [320, 390, 768, 1440]) {
    await page.setViewportSize({ width, height: width < 600 ? 844 : 1100 });
    await page.waitForTimeout(200);
    assert.ok(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth + 1,
      ),
      `Page overflows at ${width}px`,
    );
    const boats = await page.locator(".rv-lane").all();
    assert.equal(boats.length, 5);
    for (const boat of boats) {
      const bounds = await boat.boundingBox();
      assert.ok(
        bounds.x >= 0 && bounds.x + bounds.width <= width + 1,
        `Boat clipped at ${width}px`,
      );
    }
    await page.screenshot({ path: path.join(output, `width-${width}.png`) });
    if (width === 390)
      await page.screenshot({
        path: path.join(output, "mobile-full.png"),
        fullPage: true,
      });
  }
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.locator('[data-mode="projection"]').click();
  const animations = await page.evaluate(
    () =>
      [...document.querySelectorAll("#riverHero *")].filter((node) => {
        const style = getComputedStyle(node);
        return (
          style.animationName !== "none" && style.animationDuration !== "0s"
        );
      }).length,
  );
  assert.equal(animations, 0, "Reduced motion must stop decorative animations");
  assert.deepEqual(errors, [], "Browser runtime errors");
  console.log(
    `PASS: desktop/mobile 320–1440px, assets, reduced motion, no runtime errors. Screenshots: ${output}`,
  );
  if (process.env.V2_VERIFY_LIVE_API === "1") {
    let liveData;
    await page.unroute("**/api/**");
    await page.route("**/api/**", async (route) => {
      const source = new URL(route.request().url());
      const response = await route.fetch({
        url: `https://brabant-royale.vercel.app${source.pathname}${source.search}`,
      });
      if (source.pathname.includes("test-clan-prototype"))
        liveData = await response.json();
      await route.fulfill({ response });
    });
    await page.emulateMedia({ reducedMotion: "no-preference" });
    await page.goto("http://127.0.0.1:8765/v2/");
    await page.waitForFunction(
      () => document.querySelectorAll(".rv-lane").length > 0,
      null,
      { timeout: 60000 },
    );
    assert.ok(liveData?.ok, "Official live API returned an error");
    assert.equal(
      await page.locator(".rv-lane").count(),
      liveData.overview_rows.length,
    );
    for (const row of liveData.overview_rows) {
      const name = row.name;
      assert.ok(
        (await page.locator("#overview").textContent()).includes(name),
        `Live clan ${name} missing`,
      );
    }
    await page.screenshot({ path: path.join(output, "live-api-desktop.png") });
    assert.deepEqual(errors, [], "Live-data runtime errors");
    console.log(
      `PASS: live production API payload rendered through V2 fetch pipeline (${liveData.overview_rows.length} clans, ${liveData.players.length} players).`,
    );
  }
  await browser.close();
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
