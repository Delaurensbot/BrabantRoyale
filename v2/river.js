(() => {
  "use strict";
  const host = document.getElementById("riverHero");
  if (!host) return;
  let current = null,
    mode = "live",
    selected = null;
  const numeric = (value) =>
    value !== null &&
    value !== undefined &&
    value !== "" &&
    Number.isFinite(Number(value))
      ? Number(value)
      : null;
  const fmt = (value) =>
    numeric(value) === null
      ? "—"
      : Number(value).toLocaleString("nl-NL", { maximumFractionDigits: 1 });
  const tag = (value) =>
    String(value || "")
      .replace(/^#/, "")
      .toUpperCase();
  const el = (name, cls, text) => {
    const node = document.createElement(name);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  host.innerHTML = `<div class="rv-shell"><nav class="rv-nav" aria-label="Race navigatie"><a href="/v2/" class="rv-brand"><span aria-hidden="true">♛</span> BRABANT ROYALE <small>V2</small></a><div><a href="#riverHero" aria-current="page">Race</a><a href="#dashboard">Dashboard</a><a href="/">Origineel ↗</a></div></nav><header class="rv-heading"><div><div class="rv-eyebrow">CLAN WAR CONTROL</div><h1>River Race</h1></div><div class="rv-switch" role="group" aria-label="Scoreweergave"><button type="button" data-mode="live" aria-pressed="true">Huidige stand</button><button type="button" data-mode="projection" aria-pressed="false">Projection <span aria-hidden="true">↗</span></button></div></header><div class="rv-layout"><div class="rv-arena"><div class="rv-scene"><div class="rv-water-light" aria-hidden="true"></div><div class="rv-scene-top"><span class="rv-scope">OFFICIËLE API</span><span class="rv-mode-label">HUIDIGE STAND</span></div><div class="rv-lanes"></div><div class="rv-state" role="status">Officiële racedata ophalen…</div><div class="rv-scene-caption">Relatieve score · geen in-game afstand</div></div><div class="rv-dock" aria-label="Alle clans"></div></div><aside class="rv-sidebar"><section class="rv-outlook"><div class="rv-card-eyebrow">JOUW CLAN · VERWACHTING</div><div class="rv-outlook-main"><strong>—</strong><span>Nog geen officiële data</span></div><div class="rv-outlook-grid"></div><p class="rv-model-note">Lokale berekening uit de officiële API.</p><a href="#strategy">Bekijk de strategie <span aria-hidden="true">→</span></a></section><section class="rv-profile" aria-label="Geselecteerde clan"><div class="rv-card-eyebrow">CLAN IN BEELD</div><h2>Selecteer een boot</h2><div class="rv-profile-tag">Bekijk aanvallen, rendement en verwachting.</div><dl></dl></section><div class="rv-source"><span aria-hidden="true">◇</span><p class="rv-explainer">Scores komen uit de officiële Clash Royale API. De verwachting is een berekening, geen gegarandeerde eindstand.</p></div></aside></div><a class="rv-dashboard-link" href="#dashboard">Het volledige clanoverzicht <span aria-hidden="true">↓</span></a></div>`;
  const lanes = host.querySelector(".rv-lanes"),
    dock = host.querySelector(".rv-dock"),
    state = host.querySelector(".rv-state");
  const score = (row, field) =>
    row.score_available === false ? null : numeric(row[field]);
  const rows = () => {
    const result = new Map();
    (Array.isArray(current?.overview_rows)
      ? current.overview_rows
      : []
    ).forEach((row, i) => {
      if (row && typeof row === "object")
        result.set(tag(row.tag) || `untagged-${i}`, row);
    });
    return [...result].map(([key, row]) => ({ key, row }));
  };
  function profile(entries) {
    const active = entries.find((item) => item.key === selected),
      panel = host.querySelector(".rv-profile");
    panel.querySelector("h2").textContent =
      active?.row.name || "Selecteer een boot";
    panel.querySelector(".rv-profile-tag").textContent = active
      ? active.row.tag || "Clantag niet beschikbaar"
      : "Bekijk aanvallen, rendement en verwachting.";
    const list = panel.querySelector("dl");
    list.replaceChildren();
    if (active) {
      const row = active.row;
      [
        ["Huidige score", fmt(score(row, "medals"))],
        ["Verwachte score", fmt(score(row, "projected_medals"))],
        [
          "Aanvallen gespeeld",
          `${fmt(row.decks_used_today)} / ${fmt(row.decks_total_today)}`,
        ],
        ["Gemiddeld per deck", fmt(row.avg_medals_per_deck)],
      ].forEach(([label, value]) => {
        const group = el("div");
        group.append(el("dt", "", label), el("dd", "", value));
        list.append(group);
      });
    }
    host
      .querySelectorAll("[data-clan]")
      .forEach((button) =>
        button.setAttribute(
          "aria-pressed",
          String(button.dataset.clan === selected),
        ),
      );
    lanes
      .querySelectorAll(".rv-lane")
      .forEach((lane) =>
        lane.classList.toggle("rv-selected", lane.dataset.key === selected),
      );
  }
  function clear(message) {
    current = null;
    selected = null;
    lanes.replaceChildren();
    dock.replaceChildren();
    state.textContent = message;
    state.hidden = false;
    host
      .querySelector(".rv-outlook-main")
      .replaceChildren(
        el("strong", "", "—"),
        el("span", "", "Nog geen officiële data"),
      );
    host.querySelector(".rv-outlook-grid").replaceChildren();
    host.querySelector(".rv-scope").textContent = "OFFICIËLE API";
    host.querySelector(".rv-model-note").textContent =
      "Lokale berekening uit de officiële API.";
    profile([]);
  }
  function render() {
    const entries = rows();
    if (!entries.length) {
      clear("Er is nog geen officiële race-informatie beschikbaar.");
      return;
    }
    state.hidden = true;
    if (!entries.some((item) => item.key === selected))
      selected =
        entries.find((item) => tag(item.row.tag) === tag(current.clan_tag))
          ?.key || entries[0].key;
    const field = mode === "live" ? "medals" : "projected_medals";
    const maximum = Math.max(
      1,
      ...entries.flatMap(({ row }) => [
        score(row, "medals") ?? 0,
        score(row, "projected_medals") ?? 0,
      ]),
    );
    const cumulative = entries.some(
      ({ row }) => row.score_scope === "colosseum_cumulative",
    );
    host.querySelector(".rv-scope").textContent = cumulative
      ? "COLOSSEUM · CUMULATIEVE SCORE"
      : "RIVER RACE · DAGSCORE";
    host.querySelector(".rv-mode-label").textContent =
      mode === "live" ? "HUIDIGE STAND" : "PROJECTION · SCHATTING";
    host.querySelector(".rv-model-note").textContent = cumulative
      ? "Schatting inclusief resterende Colosseum-dagen."
      : "Schatting voor deze racedag.";
    host.querySelector(".rv-explainer").textContent =
      "Officiële API-scores. Projectie: huidige score + resterende decks × gemiddeld punten per deck. " +
      (cumulative
        ? "Cumulatieve Colosseum-score, inclusief resterende dagen."
        : "Dagscore van deze river race.") +
      " Geen gegarandeerde eindstand.";
    lanes.style.setProperty("--clans", entries.length);
    const existing = new Map(
      [...lanes.children].map((node) => [node.dataset.key, node]),
    );
    dock.replaceChildren();
    entries.forEach(({ key, row }, index) => {
      const own = tag(row.tag) === tag(current.clan_tag),
        value = score(row, field);
      const rank =
        value === null
          ? "—"
          : String(
              1 +
                entries.filter(
                  (item) =>
                    score(item.row, field) !== null &&
                    score(item.row, field) > value,
                ).length,
            );
      let lane = existing.get(key);
      if (!lane) {
        lane = el("div", "rv-lane");
        lane.dataset.key = key;
        const vessel = el("div", "rv-vessel"),
          button = el("button", "rv-boat");
        button.type = "button";
        button.dataset.clan = key;
        const wake = el("span", "rv-wake");
        wake.setAttribute("aria-hidden", "true");
        const image = el("img", "rv-boat-image");
        image.src = "/v2/assets/royal-boat.webp";
        image.alt = "";
        image.draggable = false;
        const badge = el("span", "rv-boat-badge");
        badge.setAttribute("aria-hidden", "true");
        button.append(wake, image, badge);
        button.addEventListener("click", () => {
          selected = key;
          profile(rows());
        });
        const metrics = el("div", "rv-metrics");
        metrics.append(
          el("strong", "rv-clan-name"),
          el("strong", "rv-score"),
          el("span", "rv-attacks"),
          el("span", "rv-average"),
        );
        vessel.append(button, metrics);
        lane.append(vessel);
        lanes.append(lane);
      }
      existing.delete(key);
      lane.classList.toggle("rv-own", own);
      lane.style.setProperty("--boat-delay", `${index * -0.7}s`);
      lane.querySelector(".rv-boat-image").src = own
        ? "/v2/assets/royal-boat.webp"
        : "/v2/assets/rival-boat.webp";
      const color = own
        ? "#65caff"
        : ["#f09182", "#dfacf5", "#ffdd72", "#b7ee9a"][index % 4];
      lane.style.setProperty("--clan-color", color);
      lane
        .querySelector(".rv-boat")
        .setAttribute(
          "aria-label",
          `${row.name || "Clan"}, plaats ${rank}, ${fmt(value)} punten. Bekijk clandetails`,
        );
      lane.querySelector(".rv-boat-badge").textContent = rank;
      lane.querySelector(".rv-clan-name").textContent =
        row.name || "Onbekende clan";
      lane.querySelector(".rv-score").textContent =
        `${fmt(value)} ${mode === "projection" ? "verw. " : ""}punten`;
      lane.querySelector(".rv-attacks").textContent =
        `${fmt(row.decks_used_today)} / ${fmt(row.decks_total_today)} aanvallen`;
      lane.querySelector(".rv-average").textContent =
        `${fmt(row.avg_medals_per_deck)} pnt / deck`;
      requestAnimationFrame(() =>
        lane.style.setProperty(
          "--progress",
          value === null ? "0" : String(Math.max(0, value / maximum)),
        ),
      );
      const item = el("button", "rv-dock-row");
      item.type = "button";
      item.dataset.clan = key;
      item.style.setProperty("--clan-color", color);
      const name = el("span", "rv-dock-name");
      name.append(
        el("strong", "", row.name || "Onbekende clan"),
        el(
          "small",
          "",
          `${own ? "JOUW CLAN · " : ""}${fmt(row.decks_used_today)} / ${fmt(row.decks_total_today)} aanvallen · ${fmt(row.avg_medals_per_deck)} pnt/deck`,
        ),
      );
      const points = el("span", "rv-dock-points");
      points.append(
        el("strong", "", fmt(value)),
        el("small", "", mode === "projection" ? "verw. punten" : "punten"),
      );
      item.append(el("span", "rv-dock-rank", rank), name, points);
      item.addEventListener("click", () => {
        selected = key;
        profile(rows());
      });
      dock.append(item);
    });
    existing.forEach((node) => node.remove());
    const finish = current.finish_outlook || {};
    if (numeric(finish.projected_finish) === null) {
      host.querySelector(".rv-model-note").textContent =
        "Voor een verwachting zijn officiële dagpunten nodig.";
    }
    host
      .querySelector(".rv-outlook-main")
      .replaceChildren(
        el(
          "strong",
          "",
          numeric(finish.projected_rank) === null
            ? "—"
            : `#${fmt(finish.projected_rank)}`,
        ),
        el("span", "", `${fmt(finish.projected_finish)} verwachte punten`),
      );
    const grid = host.querySelector(".rv-outlook-grid");
    grid.replaceChildren();
    [
      ["Beste", finish.best_rank, finish.best_finish],
      ["Laagste", finish.worst_rank, finish.worst_finish],
    ].forEach(([label, rank, points]) => {
      const cell = el("div");
      cell.append(
        el("small", "", label),
        el("strong", "", numeric(rank) === null ? "—" : `#${fmt(rank)}`),
        el("span", "", `${fmt(points)} punten`),
      );
      grid.append(cell);
    });
    const remaining = el("div", "rv-remaining");
    remaining.append(
      el("small", "", "Aanvallen over"),
      el("strong", "", fmt(finish.battles_left)),
    );
    grid.append(remaining);
    profile(entries);
  }
  host.querySelectorAll("[data-mode]").forEach((button) =>
    button.addEventListener("click", () => {
      mode = button.dataset.mode;
      host
        .querySelectorAll("[data-mode]")
        .forEach((item) =>
          item.setAttribute("aria-pressed", String(item === button)),
        );
      if (current) render();
    }),
  );
  const visibility = () => host.classList.toggle("rv-paused", document.hidden);
  document.addEventListener("visibilitychange", visibility);
  visibility();
  if ("IntersectionObserver" in window)
    new IntersectionObserver(([entry]) =>
      host.classList.toggle("rv-offscreen", !entry.isIntersecting),
    ).observe(host);
  window.RiverV2 = {
    update(data) {
      current = data && typeof data === "object" ? data : {};
      render();
    },
    loading(clanTag) {
      clear(`Officiële racedata ophalen${clanTag ? " voor " + clanTag : ""}…`);
    },
    error(message) {
      clear(
        message ||
          "Officiële racedata kon niet worden geladen. Probeer opnieuw.",
      );
    },
  };
})();
