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
  const target = process.env.V2_TEST_URL || "http://127.0.0.1:8765/";
  const original = await page.goto(new URL("/classic/", target).href);
  assert.equal(original.status(), 200, "Classic backup route must load");
  assert.equal(await page.locator("#riverHero").count(), 0);
  assert.equal(
    await page.locator(".card").count(),
    11,
    "Classic dashboard preserved",
  );
  const alias = await page.goto(new URL("/v2/", target).href);
  assert.equal(alias.status(), 200, "V2 alias must stay reachable");
  await page.waitForFunction(() => typeof window.RiverV2 === "object");
  assert.equal(await page.locator(".rv-brand").getAttribute("href"), "/");
  assert.equal(
    await page.getByRole("link", { name: "Origineel" }).getAttribute("href"),
    "/classic/",
  );
  const home = await page.goto(target);
  assert.equal(home.status(), 200, "V2 homepage must load");
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
    await page.evaluate(() => {
      const base = window.__v2Fixture;
      const check = (condition, message) => {
        if (!condition) throw new Error(message);
      };
      for (const phase of [
        "colosseum",
        "training",
        "colosseum-training",
        "practice",
        "unknown",
        "warDay",
      ]) {
        RiverV2.update({
          ...base,
          race_state: {
            period_type: phase === "colosseum-training" ? "training" : phase,
            period_index: 23,
            battle_day: phase === "warDay" ? 3 : null,
            is_colosseum_weekend: phase === "training",
          },
          week_context:
            phase === "colosseum-training" ? { mode: "colosseum" } : undefined,
        });
        const hero = document.getElementById("riverHero");
        const expected =
          phase === "warDay"
            ? "race"
            : ["training", "practice", "colosseum-training"].includes(phase)
              ? "practice"
              : phase;
        check(hero.dataset.phase === expected, `Incorrect phase ${phase}`);
        check(
          document.querySelector('[data-mode="projection"]').disabled ===
            ["practice", "unknown"].includes(expected),
          `Projection control ${phase}`,
        );
        const image = getComputedStyle(
          document.querySelector(".rv-scene"),
        ).backgroundImage;
        check(
          image.includes(
            expected === "colosseum" || phase === "colosseum-training"
              ? "colosseum-world"
              : "river-world",
          ),
          `Wrong scene ${phase}`,
        );
        if (expected === "practice") {
          check(
            document
              .querySelector(".rv-leader")
              .textContent.includes("Trainingsdag 3"),
            "Training day absent",
          );
          check(
            [...document.querySelectorAll(".rv-boat-badge")].every(
              (node) => node.textContent === "—",
            ),
            "Training ranks leaked",
          );
        }
        check(
          document.documentElement.scrollWidth <= innerWidth + 1,
          `Phase ${phase} overflows`,
        );
      }
      RiverV2.update(base);
    });
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
  for (const width of [320, 390, 768, 1440]) {
    await page.setViewportSize({ width, height: width < 600 ? 844 : 1100 });
    // Worst case: every clan open and every projected boat at the front.
    await page.evaluate(() => {
      const data = window.__v2WarningFixture;
      RiverV2.update({
        ...data,
        overview_rows: data.overview_rows.map((row) => ({
          ...row,
          medals: 10000,
          projected_medals: 20000,
          clan_access: { type: "open", is_open: true, source: "official_api", members: 49 },
        })),
      });
      document.querySelector('[data-mode="projection"]').click();
    });
    await page.waitForTimeout(1800);
    const scene = await page.locator(".rv-scene").boundingBox();
    const leader = await page.locator(".rv-leader").boundingBox();
    const badges = await page.locator(".rv-open-warning:not([hidden])").all();
    assert.equal(badges.length, 5, "All five open clans must be marked");
    for (const badge of badges) {
      const bounds = await badge.boundingBox();
      assert.ok(
        bounds.x >= scene.x && bounds.x + bounds.width <= scene.x + scene.width,
        `Open warning clipped at ${width}px`,
      );
      // Allow the complete 6px floating range, even when sampled at its bottom.
      assert.ok(
        bounds.y - 6 >= leader.y + leader.height,
        `Open warning overlaps leader at ${width}px`,
      );
    }
    await page.screenshot({
      path: path.join(output, `open-clans-${width}.png`),
    });
    await page.locator('[data-mode="live"]').click();
  }
  for (const width of [390, 1440]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1100 });
    for (const phase of [
      "colosseum",
      "training",
      "colosseum-training",
      "loading",
    ]) {
      await page.evaluate((phase) => {
        if (phase === "loading") {
          RiverV2.loading("9YP8UY");
          return;
        }
        RiverV2.update({
          ...window.__v2Fixture,
          race_state: {
            period_type: phase === "colosseum-training" ? "training" : phase,
            period_index: 23,
            battle_day: phase === "colosseum" ? 3 : null,
          },
          week_context:
            phase === "colosseum-training" ? { mode: "colosseum" } : undefined,
        });
      }, phase);
      await page.evaluate(async () => {
        const background = getComputedStyle(
          document.querySelector(".rv-scene"),
        ).backgroundImage;
        const source = background.match(/url\(["']?([^"')]+)["']?\)/)?.[1];
        if (source) {
          const image = new Image();
          image.src = source;
          await image.decode();
        }
      });
      await page.waitForTimeout(1800);
      await page.screenshot({
        path: path.join(output, `${phase}-${width}.png`),
        fullPage: false,
      });
      if (width === 390)
        await page.screenshot({
          path: path.join(output, `${phase}-${width}-full.png`),
          fullPage: true,
        });
    }
  }
  await page.evaluate(() => RiverV2.update(window.__v2WarningFixture));
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
  await page.evaluate(() => RiverV2.loading("9YP8UY"));
  assert.equal(
    await page
      .locator(".rv-loader i")
      .first()
      .evaluate((node) => getComputedStyle(node).animationName),
    "none",
    "Reduced motion must also stop the loading indicator",
  );
  await page.evaluate(() => RiverV2.update(window.__v2Fixture));
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
    await page.goto(target);
    await page.waitForFunction(
      () => document.querySelectorAll(".rv-lane").length > 0,
      null,
      { timeout: 60000 },
    );
    assert.ok(liveData?.ok, "Official live API returned an error");
    const type = String(liveData.race_state?.period_type || "")
      .toLowerCase()
      .replace(/[\s_-]/g, "");
    const training = [
      "training",
      "practice",
      "trainingday",
      "practiceday",
    ].includes(type);
    const expectedPhase = training
      ? "practice"
      : type === "colosseum" ||
          liveData.race_state?.is_colosseum_weekend === true
        ? "colosseum"
        : type === "warday"
          ? "race"
          : "unknown";
    assert.equal(
      await page.locator("#riverHero").getAttribute("data-phase"),
      expectedPhase,
    );
    assert.equal(
      await page.locator('[data-mode="projection"]').isDisabled(),
      ["practice", "unknown"].includes(expectedPhase),
    );
    if (training) {
      assert.ok(
        (await page.locator(".rv-leader").textContent()).includes(
          "GEEN WEDSTRIJD",
        ),
      );
      assert.ok(
        (await page.locator(".rv-boat-badge").allTextContents()).every(
          (rank) => rank === "—",
        ),
      );
    }
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
    await page.getByRole("link", { name: "Origineel" }).click();
    await page.waitForURL("**/classic/");
    await page.waitForFunction(
      () =>
        document.getElementById("updated").textContent.trim() !== "-" &&
        document.querySelector("#overview tbody tr"),
      null,
      { timeout: 60000 },
    );
    assert.equal(await page.locator("#riverHero").count(), 0);
    for (const row of liveData.overview_rows)
      assert.ok(
        (await page.locator("#overview").textContent()).includes(row.name),
        `Classic live clan ${row.name} missing after load`,
      );
    assert.equal(await page.locator(".card").count(), 11);
    assert.deepEqual(errors, [], "Classic live-data runtime errors");
    console.log(
      "PASS: Origineel navigation loads the retained classic dashboard with live API data.",
    );
  }
  await browser.close();
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
