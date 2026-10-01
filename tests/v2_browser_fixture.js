// Browser-only fixture: pipe into agent-browser eval --stdin on a local V2 page.
// Never loaded by the deployed site. Exercises the real fetch/render pipeline.
(async () => {
  const assert = (value, message) => {
    if (!value) throw new Error(message);
  };
  const names = [
    "Brabant Royale",
    "Royal Guardians",
    "The Last Kingdom",
    "Les Mousquetaires",
    "Nordic Vikings",
  ];
  const tags = ["#9YP8UY", "#AAA", "#BBB", "#CCC", "#DDD"];
  const scores = [18200, 20100, 17000, 14900, 13100];
  const projections = [33800, 30400, 29100, 27000, 25400];
  const rows = names.map((name, i) => ({
    name,
    tag: tags[i],
    medals: scores[i],
    boat_points: scores[i] * 2,
    decks_used_today: 100 + i * 8,
    decks_used_total: 300 + i * 8,
    decks_total_today: 200,
    avg_medals_per_deck: 182 - i * 9,
    projected_medals: projections[i],
    score_available: true,
    score_scope: "river_race_day",
  }));
  const data = {
    ok: true,
    clan_tag: "9YP8UY",
    clan: { name: names[0] },
    overview_rows: rows,
    players: [],
    race_state: {
      period_type: "warDay",
      is_colosseum_weekend: false,
      battle_day: 3,
    },
    finish_outlook: {
      projected_rank: 1,
      projected_finish: 33800,
      best_rank: 1,
      best_finish: 38000,
      worst_rank: 3,
      worst_finish: 27000,
      battles_left: 100,
      duels_left: 12,
      total_players_participated: 30,
      projection_scope: "river_race_day",
    },
  };
  window.__v2Fixture = data;
  window.fetch = async (url) => ({
    ok: true,
    json: async () =>
      String(url).includes("test-clan-prototype") ? data : { ok: true },
  });
  await fetchData();
  const hero = document.getElementById("riverHero");
  assert(
    hero.getAttribute("aria-busy") === "false",
    "Loading must finish after data",
  );
  const normalFetch = window.fetch;
  let releaseFetch;
  const delayed = new Promise((resolve) => {
    releaseFetch = resolve;
  });
  window.fetch = async (url) => {
    await delayed;
    return normalFetch(url);
  };
  const refresh = fetchData();
  assert(
    hero.getAttribute("aria-busy") === "true",
    "Real refresh must mark loading",
  );
  assert(
    !document.querySelector(".rv-loader").hidden,
    "Loading indicator visible",
  );
  assert(
    document.querySelectorAll(".rv-loader i").length === 3,
    "Clean three-dot loader",
  );
  releaseFetch();
  await refresh;
  window.fetch = normalFetch;
  assert(hero.getAttribute("aria-busy") === "false", "Refresh stops loading");
  assert(
    document.querySelector(".rv-loader").hidden,
    "Completed request hides loader",
  );
  assert(
    document.querySelectorAll(".rv-lane").length === 5,
    "Five boats must render",
  );
  assert(
    document.querySelector(".rv-attacks").textContent.includes("100"),
    "Attack usage missing",
  );
  assert(
    document.querySelector(".rv-average").textContent.includes("182"),
    "Average missing",
  );
  assert(
    document.getElementById("overview").textContent.includes("Brabant Royale"),
    "Existing overview missing",
  );
  assert(
    document.querySelectorAll("#dashboard .card").length === 11,
    "Legacy cards not preserved",
  );
  document.querySelector('[data-mode="projection"]').click();
  assert(
    document.querySelector(".rv-leader").textContent.includes(names[1]),
    "Projection must retain the current actual leader",
  );
  assert(
    document.querySelector(".rv-dock-row").dataset.clan === "9YP8UY",
    "Projected dock must rank projected scores",
  );
  assert(
    document.querySelector(".rv-score").textContent.includes("33.800"),
    "Projection did not update",
  );
  document.querySelector('[data-mode="live"]').click();
  assert(
    document.querySelector(".rv-score").textContent.includes("18.200"),
    "Live did not restore",
  );
  RiverV2.update({
    ...data,
    overview_rows: [
      {
        ...rows[0],
        medals: null,
        projected_medals: null,
        avg_medals_per_deck: null,
        score_available: false,
      },
    ],
  });
  assert(
    document.querySelector(".rv-score").textContent.includes("—"),
    "Missing score displayed as zero",
  );
  RiverV2.loading("GPCLVLPP");
  assert(!document.querySelector(".rv-lane"), "Stale boats after clan switch");
  RiverV2.error("Fixture error");
  assert(hero.getAttribute("aria-busy") === "false", "Error stops busy state");
  assert(
    document.querySelector(".rv-loader").hidden,
    "Error is not an endless spinner",
  );
  assert(
    document.querySelector(".rv-state").textContent === "Fixture error",
    "Error not shown",
  );
  RiverV2.update({
    ...data,
    race_state: { is_colosseum_weekend: true },
    overview_rows: rows.map((row) => ({
      ...row,
      score_scope: "colosseum_cumulative",
    })),
  });
  assert(
    document.querySelector(".rv-scope").textContent.includes("CUMULATIEVE"),
    "Colosseum scope missing",
  );
  RiverV2.update(data);
  const tied = {
    ...data,
    overview_rows: rows.map((row) => ({
      ...row,
      medals: 10000,
      projected_medals: 10000,
    })),
  };
  RiverV2.update(tied);
  assert(
    [...document.querySelectorAll(".rv-boat-badge")].every(
      (badge) => badge.textContent === "1",
    ),
    "Equal scores must share rank",
  );
  document.querySelector(".rv-lane:nth-child(2) .rv-boat").click();
  assert(
    document.querySelector(".rv-profile h2").textContent === names[1],
    "Boat selection must show the selected clan",
  );
  assert(
    document.querySelector(".rv-profile dl").textContent.includes("108"),
    "Selected clan attacks incorrect",
  );
  RiverV2.update({ ...data, overview_rows: [] });
  assert(
    !document.querySelector(".rv-lane"),
    "Empty API response must clear all boats",
  );
  RiverV2.update(data);
  assert(
    document.querySelector(".rv-leader").textContent.includes(names[1]),
    "Current leader must use live points",
  );
  assert(
    document.querySelector(".rv-dock-row").dataset.clan === "AAA",
    "Dock must be ranked",
  );
  RiverV2.update({
    ...data,
    race_state: {
      period_type: "training",
      period_index: 23,
      battle_day: null,
      is_colosseum_weekend: true,
    },
  });
  assert(
    document.querySelector("#riverHero").dataset.phase === "practice",
    "Training must override contradictory Colosseum flag",
  );
  assert(
    document.querySelector(".rv-leader").textContent.includes("Trainingsdag 3"),
    "Training day derived from official period index",
  );
  assert(
    document.querySelector('[data-mode="projection"]').disabled,
    "Training projection disabled",
  );
  assert(
    [...document.querySelectorAll(".rv-boat-badge")].every(
      (node) => node.textContent === "—",
    ),
    "Training must not rank",
  );
  document.querySelector('[data-mode="projection"]').click();
  assert(
    document
      .querySelector('[data-mode="live"]')
      .getAttribute("aria-pressed") === "true",
    "Training cannot enter projection",
  );
  RiverV2.update({
    ...data,
    race_state: { period_type: "training", period_index: 23 },
    week_context: {
      mode: "colosseum",
      source: "official_api_indices_and_season_calendar",
      week: 4,
      season_weeks: 4,
    },
  });
  assert(
    hero.dataset.phase === "practice",
    "Colosseum preparation stays training",
  );
  assert(
    hero.dataset.world === "colosseum",
    "Colosseum art is already shown during training",
  );
  assert(
    document
      .querySelector(".rv-leader")
      .textContent.includes("Trainingsdag 3 voor Colosseum"),
    "Preparation banner missing",
  );
  assert(
    document
      .querySelector(".rv-outlook-grid")
      .textContent.includes("4 strijddagen"),
    "Upcoming weekend information missing",
  );
  assert(
    document.querySelector('[data-mode="projection"]').disabled,
    "Preparation cannot activate projection",
  );
  assert(
    [...document.querySelectorAll(".rv-boat-badge")].every(
      (node) => node.textContent === "—",
    ),
    "Preparation cannot activate ranking",
  );
  assert(
    !document
      .querySelector(".rv-profile dl")
      .textContent.includes("Aanvallen cumulatief"),
    "Preparation must not reuse competitive cumulative stats",
  );
  RiverV2.update({
    ...data,
    race_state: { period_type: "training", period_index: 23 },
    week_context: { mode: "river_race" },
  });
  assert(
    hero.dataset.world === "river",
    "Regular training must restore river art",
  );
  assert(
    !document.querySelector(".rv-outlook-grid").textContent,
    "Regular training clears Colosseum details",
  );
  RiverV2.update({ ...data, race_state: { period_type: "unknown" } });
  assert(
    document.querySelector("#riverHero").dataset.phase === "unknown",
    "Unknown phase stays neutral",
  );
  assert(
    document.querySelector('[data-mode="projection"]').disabled,
    "Unknown phase projection disabled",
  );
  RiverV2.update({ ...data, race_state: { period_type: "colosseum" } });
  assert(
    document.querySelector("#riverHero").dataset.phase === "colosseum",
    "Official period type determines Colosseum",
  );
  assert(
    document.querySelector(".rv-average").textContent.includes("alle decks"),
    "Colosseum average scope visible",
  );
  assert(
    document
      .querySelector(".rv-profile dl")
      .textContent.includes("Aanvallen cumulatief"),
    "Colosseum cumulative attacks visible",
  );
  RiverV2.loading("AAA");
  assert(
    document.querySelector("#riverHero").dataset.phase === "unknown",
    "Loading clears stale theme",
  );
  assert(
    !document.querySelector(".rv-leader").textContent,
    "Loading clears stale leader",
  );
  RiverV2.update({
    ...data,
    overview_rows: rows.map((row, i) => ({
      ...row,
      medals: i === 0 ? null : 0,
    })),
  });
  assert(
    document.querySelector(".rv-boat-badge").textContent === "—",
    "Null is not rankable",
  );
  assert(
    [...document.querySelectorAll(".rv-boat-badge")]
      .slice(1)
      .every((node) => node.textContent === "1"),
    "Zero scores tie honestly",
  );
  RiverV2.update(data);
  for (const period_type of ["training", "colosseum"]) {
    RiverV2.update({
      ...data,
      overview_rows: [],
      race_state: { period_type, period_index: 23 },
    });
    assert(
      document.getElementById("riverHero").dataset.phase ===
        (period_type === "training" ? "practice" : "colosseum"),
      "Empty data must preserve official phase",
    );
    assert(
      !document.querySelector(".rv-lane") &&
        !document.querySelector(".rv-leader").textContent,
      "Empty phase clears boats and leader",
    );
    assert(
      hero.getAttribute("aria-busy") === "false" &&
        document.querySelector(".rv-loader").hidden,
      "Empty data is not loading",
    );
  }
  RiverV2.update(data);
  const warningData = {
    ...data,
    overview_rows: rows.map((row, i) => ({
      ...row,
      clan_access:
        i === 4
          ? { type: null, is_open: null, source: "unknown" }
          : {
              type: ["inviteOnly", "open", "inviteOnly", "closed"][i],
              is_open: i === 1,
              members: 49,
              source: "official_api",
            },
    })),
  };
  window.__v2WarningFixture = warningData;
  const warnings = () =>
    document.querySelectorAll(".rv-open-warning:not([hidden])").length;
  for (const [type, period, count] of [
    ["training", 21, 0],
    ["training", 22, 0],
    ["training", 23, 2],
    ["training", null, 0],
    ["unknown", 23, 0],
    ["warDay", 24, 2],
    ["warDay", 25, 2],
    ["warDay", 26, 2],
    ["warDay", 27, 2],
    ["colosseum", 24, 2],
    ["colosseum", 27, 2],
  ]) {
    RiverV2.update({
      ...warningData,
      race_state: { period_type: type, period_index: period },
    });
    assert(
      warnings() === count,
      `Wrong open warnings during ${type} / ${period}`,
    );
    assert(
      document.querySelectorAll(".rv-open-label").length === count,
      "Dock warning count must match boats",
    );
  }
  RiverV2.update(warningData);
  assert(
    document
      .querySelector(".rv-boat")
      .getAttribute("aria-label")
      .includes("Deze clan staat op uitnodiging en heeft minder dan 50 leden"),
    "Warning must be accessible on boat",
  );
  assert(
    document
      .querySelector(".rv-profile dl")
      .textContent.includes("Alleen op uitnodiging · minder dan 50 leden"),
    "Open status missing in selected-clan details",
  );
  document.querySelector('[data-mode="projection"]').click();
  assert(warnings() === 2, "Projection must retain open status");
  document.querySelector('.rv-dock-row[data-clan="AAA"]').click();
  assert(
    document
      .querySelector(".rv-profile dl")
      .textContent.includes("Open"),
    "Open is distinct from invite-only",
  );
  document.querySelector('.rv-dock-row[data-clan="DDD"]').click();
  assert(
    document.querySelector(".rv-profile dl").textContent.includes("Onbekend"),
    "Failed lookup is not closed",
  );
  for (const invalid of [
    ...[50, null, undefined, "49", -1, 49.5].map((members) => ({ type: "inviteOnly", is_open: false, source: "official_api", members })),
    { type: "inviteOnly", is_open: false, source: "scraper", members: 49 },
    { type: "open", is_open: true, source: "scraper" },
    { type: "open", is_open: false, source: "official_api" },
    undefined,
    { type: "closed", is_open: false, source: "official_api" },
  ]) {
    RiverV2.update({
      ...warningData,
      overview_rows: warningData.overview_rows.map((row) => ({
        ...row,
        clan_access: invalid,
      })),
    });
    assert(
      warnings() === 0,
      "Changed/unknown/untrusted status must remove old warning",
    );
  }
  RiverV2.update(warningData);
  RiverV2.loading("OTHER");
  assert(warnings() === 0, "Loading must clear previous clan warnings");
  RiverV2.error("Fixture warning reset");
  assert(warnings() === 0, "Error must not retain stale warnings");
  RiverV2.update(data);
  return "PASS: data integration, preserved dashboard, projection/live, Colosseum/training, loading/errors and phase-gated official invite-only clan capacity warnings";
})();
