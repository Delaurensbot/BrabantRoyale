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
  host.innerHTML = `<div class="rv-shell"><nav class="rv-nav" aria-label="Race navigatie"><a href="/" class="rv-brand"><span aria-hidden="true">♛</span> BRABANT ROYALE <small>V2</small></a><div><a href="#riverHero" aria-current="page">Race</a><a href="#dashboard">Dashboard</a><a href="/classic/">Origineel ↗</a></div></nav><header class="rv-heading"><div><div class="rv-eyebrow">CLAN WAR CONTROL</div><h1>River Race</h1></div><div class="rv-switch" role="group" aria-label="Scoreweergave"><button type="button" data-mode="live" aria-pressed="true">Huidige stand</button><button type="button" data-mode="projection" aria-pressed="false">Projection <span aria-hidden="true">↗</span></button></div></header><div class="rv-layout"><div class="rv-arena"><div class="rv-scene"><div class="rv-water-light" aria-hidden="true"></div><div class="rv-scene-top"><span class="rv-scope">OFFICIËLE API</span><span class="rv-mode-label">HUIDIGE STAND</span></div><div class="rv-lanes"></div><div class="rv-state" role="status">Officiële racedata ophalen…</div><div class="rv-scene-caption">Relatieve score · geen in-game afstand</div></div><div class="rv-dock" aria-label="Alle clans"></div></div><aside class="rv-sidebar"><section class="rv-outlook"><div class="rv-card-eyebrow">JOUW CLAN · VERWACHTING</div><div class="rv-outlook-main"><strong>—</strong><span>Nog geen officiële data</span></div><div class="rv-outlook-grid"></div><p class="rv-model-note">Lokale berekening uit de officiële API.</p><a href="#strategy">Bekijk de strategie <span aria-hidden="true">→</span></a></section><section class="rv-profile" aria-label="Geselecteerde clan"><div class="rv-card-eyebrow">CLAN IN BEELD</div><h2>Selecteer een boot</h2><div class="rv-profile-tag">Bekijk aanvallen, rendement en verwachting.</div><dl></dl></section><div class="rv-source"><span aria-hidden="true">◇</span><p class="rv-explainer">Scores komen uit de officiële Clash Royale API. De verwachting is een berekening, geen gegarandeerde eindstand.</p></div></aside></div><a class="rv-dashboard-link" href="#dashboard">Het volledige clanoverzicht <span aria-hidden="true">↓</span></a></div>`;
  const lanes = host.querySelector(".rv-lanes"),
    dock = host.querySelector(".rv-dock"),
    state = host.querySelector(".rv-state");
  const statePanel = el("div", "rv-state-panel"),
    loader = el("div", "rv-loader"),
    stateText = el("span", "rv-state-text", state.textContent);
  loader.setAttribute("aria-hidden", "true");
  for (let i = 0; i < 3; i++) loader.append(el("i"));
  statePanel.append(loader, stateText);
  state.replaceChildren(statePanel);
  const setBusy = (busy) => {
    host.setAttribute("aria-busy", String(busy));
    loader.hidden = !busy;
  };
  setBusy(true);
  const score = (row, field) =>
    row.score_available === false ? null : numeric(row[field]);
  const phase = () => {
    const state = current?.race_state || {};
    const type = String(state.period_type || state.periodType || "")
      .toLowerCase()
      .replace(/[\s_-]/g, "");
    const index = numeric(state.period_index ?? state.periodIndex);
    if (["training", "practice", "trainingday", "practiceday"].includes(type))
      return {
        kind: "practice",
        day:
          index !== null &&
          Number.isInteger(index) &&
          index >= 0 &&
          index % 7 < 3
            ? (index % 7) + 1
            : null,
        competitive: false,
        preparingColosseum: current?.week_context?.mode === "colosseum",
      };
    if (type === "colosseum" || state.is_colosseum_weekend === true)
      return {
        kind: "colosseum",
        day: numeric(state.battle_day),
        competitive: true,
      };
    if (type === "warday")
      return {
        kind: "race",
        day: numeric(state.battle_day),
        competitive: true,
      };
    return { kind: "unknown", day: null, competitive: false };
  };
  const leader = el("div", "rv-leader");
  leader.setAttribute("role", "status");
  host.querySelector(".rv-scene-top").after(leader);
  function setPhase(view) {
    host.dataset.phase = view.kind;
    host.dataset.world =
      view.kind === "colosseum" || view.preparingColosseum
        ? "colosseum"
        : "river";
    host.setAttribute(
      "aria-label",
      {
        race: "Interactieve River Race",
        colosseum: "Interactief Colosseum",
        practice: "Clan War trainingsdagen",
        unknown: "Clan War fase-informatie",
      }[view.kind],
    );
    host.querySelector(".rv-heading h1").textContent = {
      race: "River Race",
      colosseum: "Colosseum",
      practice: "Trainingsdagen",
      unknown: "Clan War",
    }[view.kind];
    if (view.preparingColosseum) {
      host.setAttribute("aria-label", "Trainingsdagen voor Colosseum weekend");
      host.querySelector(".rv-heading h1").textContent = "Colosseum · training";
    }
    const projection = host.querySelector('[data-mode="projection"]');
    projection.disabled = !view.competitive;
    projection.title = view.competitive
      ? "Bekijk de berekende eindstand"
      : "Geen competitieve projectie in deze fase";
    if (!view.competitive) mode = "live";
    host
      .querySelectorAll("[data-mode]")
      .forEach((button) =>
        button.setAttribute(
          "aria-pressed",
          String(button.dataset.mode === mode),
        ),
      );
    host.querySelector(".rv-outlook .rv-card-eyebrow").textContent =
      view.competitive ? "JOUW CLAN · VERWACHTING" : "JOUW CLAN · FASE";
    host.querySelector(".rv-scene-caption").textContent = view.competitive
      ? "Relatieve score · geen in-game afstand"
      : "Geen competitieve rangschikking in deze fase";
  }
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
  const accessType = (row) => {
    const access = row.clan_access;
    return access?.source === "official_api" &&
      ["open", "inviteOnly", "closed"].includes(access.type) &&
      access.is_open === (access.type === "open")
      ? access.type
      : null;
  };
  const openWarning = (row, view = phase()) =>
    accessType(row) === "open" &&
    row.clan_access.is_open === true &&
    (view.competitive || (view.kind === "practice" && view.day === 3));
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
        [
          "Clanstatus · API",
          {
            open: openWarning(row) ? "Open · let op" : "Open",
            inviteOnly: "Alleen op uitnodiging",
            closed: "Gesloten",
          }[accessType(row)] || "Onbekend",
        ],
        ["Huidige score", fmt(score(row, "medals"))],
        ...(phase().competitive
          ? [["Verwachte score", fmt(score(row, "projected_medals"))]]
          : []),
        [
          "Aanvallen vandaag",
          `${fmt(row.decks_used_today)} / ${fmt(row.decks_total_today)}`,
        ],
        ...(phase().kind === "colosseum"
          ? [["Aanvallen cumulatief", fmt(row.decks_used_total)]]
          : []),
        [
          phase().kind === "colosseum"
            ? "Gem. per deck · alle decks"
            : "Gemiddeld per deck",
          fmt(row.avg_medals_per_deck),
        ],
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
    mode = "live";
    setPhase(phase());
    leader.replaceChildren();
    leader.removeAttribute("title");
    selected = null;
    lanes.replaceChildren();
    dock.replaceChildren();
    setBusy(false);
    stateText.textContent = message;
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
    host.querySelector(".rv-mode-label").textContent = "WACHTEN OP DATA";
    host.querySelector(".rv-explainer").textContent =
      "De officiële API bepaalt de fase en beschikbare scores.";
    profile([]);
  }
  function render() {
    setBusy(false);
    const entries = rows();
    if (!entries.length) {
      const emptyData = current;
      clear("Er is nog geen officiële race-informatie beschikbaar.");
      current = emptyData;
      const view = phase();
      setPhase(view);
      host.querySelector(".rv-scope").textContent = {
        practice: "TRAINING · NIET COMPETITIEF",
        colosseum: "COLOSSEUM · CUMULATIEVE SCORE",
        race: "RIVER RACE · DAGSCORE",
        unknown: "FASE ONBEKEND · OFFICIËLE API",
      }[view.kind];
      return;
    }
    state.hidden = true;
    const view = phase();
    setPhase(view);
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
    const cumulative = view.kind === "colosseum";
    host.querySelector(".rv-scope").textContent = cumulative
      ? "COLOSSEUM · CUMULATIEVE SCORE"
      : view.kind === "race"
        ? "RIVER RACE · DAGSCORE"
        : view.kind === "practice"
          ? "TRAINING · NIET COMPETITIEF"
          : "FASE ONBEKEND · OFFICIËLE API";
    host.querySelector(".rv-mode-label").textContent = !view.competitive
      ? view.day === null
        ? "GEEN RANGLIJST"
        : `TRAININGSDAG ${view.day}`
      : mode === "live"
        ? [1, 2, 3, 4].includes(view.day)
          ? `STRIJDDAG ${view.day} · HUIDIGE STAND`
          : "HUIDIGE STAND"
        : "PROJECTION · SCHATTING";
    host.querySelector(".rv-model-note").textContent = cumulative
      ? "Schatting inclusief resterende Colosseum-dagen."
      : "Schatting voor deze racedag.";
    host.querySelector(".rv-explainer").textContent =
      "Officiële API-scores. Projectie: huidige score + resterende decks × gemiddeld punten per deck. " +
      (cumulative
        ? "Cumulatieve Colosseum-score, inclusief resterende dagen."
        : "Dagscore van deze river race.") +
      " Geen gegarandeerde eindstand.";
    if (!view.competitive)
      host.querySelector(".rv-explainer").textContent =
        view.kind === "practice"
          ? "Trainingsfase volgens de officiële API. Getoonde aanvallen en gemiddelden zijn API-statistieken; ze vormen geen trainingsranglijst. Er wordt geen competitieve eindstand voorspeld."
          : "De officiële API geeft geen herkenbare fase. Scores en aanvallen blijven zichtbaar, zonder rangschikking of projectie.";
    if (view.preparingColosseum) {
      host.querySelector(".rv-scope").textContent = "COLOSSEUM · VOORBEREIDING";
      host.querySelector(".rv-explainer").textContent =
        "Trainingsfase volgens de officiële API. De Colosseum-voorbereiding is afgeleid uit de API-week en de seizoenskalender. Trainingsaanvallen tellen niet als wedstrijdpunten; er is nog geen ranglijst of projectie.";
    }
    const available = entries.filter(
      ({ row }) => score(row, "medals") !== null,
    );
    const leadingScore = available.length
      ? Math.max(...available.map(({ row }) => score(row, "medals")))
      : null;
    const leaders = available.filter(
      ({ row }) => score(row, "medals") === leadingScore,
    );
    leader.replaceChildren();
    leader.append(
      el(
        "span",
        "rv-leader-eyebrow",
        view.competitive
          ? "HUIDIGE KOPLOPER"
          : view.kind === "practice"
            ? "TRAINING · GEEN WEDSTRIJD"
            : "WACHTEN OP OFFICIËLE FASE",
      ),
    );
    leader.title = view.competitive
      ? leaders.map(({ row }) => row.name || "Onbekende clan").join(" · ")
      : "";
    leader.append(
      el(
        "strong",
        "",
        !view.competitive
          ? view.kind === "practice"
            ? `Trainingsdag ${view.day ?? "—"}${view.preparingColosseum ? " voor Colosseum" : ""}`
            : "Nog geen rangschikking"
          : leaders.length
            ? leaders
                .slice(0, 2)
                .map(({ row }) => row.name || "Onbekende clan")
                .join(" · ") +
              (leaders.length > 2 ? ` + ${leaders.length - 2}` : "")
            : "Score nog niet beschikbaar",
      ),
    );
    leader.append(
      el(
        "small",
        "",
        !view.competitive
          ? view.preparingColosseum
            ? "Bereid je war decks voor op Colosseum weekend"
            : "Aanvallen en gemiddelden blijven hieronder beschikbaar"
          : leaders.length
            ? `${fmt(leadingScore)} punten${leaders.length > 1 ? " · gedeelde eerste plaats" : " · officiële huidige score"}`
            : "Ontbrekende punten worden niet als nul getoond",
      ),
    );
    lanes.style.setProperty("--clans", entries.length);
    const existing = new Map(
      [...lanes.children].map((node) => [node.dataset.key, node]),
    );
    dock.replaceChildren();
    entries.forEach(({ key, row }, index) => {
      const own = tag(row.tag) === tag(current.clan_tag),
        value = score(row, field);
      const rank =
        value === null || !view.competitive
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
        const warning = el("span", "rv-open-warning", "!");
        warning.setAttribute("aria-hidden", "true");
        warning.title = "Deze clan staat open";
        warning.hidden = true;
        button.append(wake, image, badge, warning);
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
      lane.classList.toggle(
        "rv-leading",
        view.competitive && leaders.some((item) => item.key === key),
      );
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
          `${row.name || "Clan"}, ${view.competitive ? "plaats " + rank : "geen competitieve rang"}, ${fmt(value)} punten.${openWarning(row, view) ? " Deze clan staat open." : ""} Bekijk clandetails`,
        );
      lane.querySelector(".rv-open-warning").hidden = !openWarning(row, view);
      lane.querySelector(".rv-boat-badge").textContent = rank;
      lane.querySelector(".rv-clan-name").textContent =
        row.name || "Onbekende clan";
      lane.querySelector(".rv-clan-name").title = row.name || "Onbekende clan";
      lane.querySelector(".rv-score").textContent =
        `${fmt(value)} ${mode === "projection" ? "verw. " : ""}punten`;
      lane.querySelector(".rv-attacks").textContent =
        `${fmt(row.decks_used_today)} / ${fmt(row.decks_total_today)} aanvallen`;
      lane.querySelector(".rv-average").textContent =
        `${fmt(row.avg_medals_per_deck)} pnt / deck${cumulative ? " · alle decks" : ""}`;
      requestAnimationFrame(() =>
        lane.style.setProperty(
          "--progress",
          !view.competitive
            ? "0.42"
            : value === null
              ? "0"
              : String(Math.max(0, value / maximum)),
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
          `${own ? "JOUW CLAN · " : ""}${fmt(row.decks_used_today)} / ${fmt(row.decks_total_today)} aanvallen vandaag · ${fmt(row.avg_medals_per_deck)} pnt/deck${cumulative ? " (alle decks)" : ""}`,
        ),
      );
      if (openWarning(row, view)) {
        name
          .querySelector("strong")
          .append(el("span", "rv-open-label", " ! OPEN"));
        item.title = "Deze clan staat open";
      }
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
    if (view.competitive) {
      const ordered = [...entries].sort(
        (a, b) =>
          (score(b.row, field) ?? -Infinity) -
          (score(a.row, field) ?? -Infinity),
      );
      const items = new Map(
        [...dock.children].map((node) => [node.dataset.clan, node]),
      );
      ordered.forEach(({ key }) => dock.append(items.get(key)));
    }
    if (!view.competitive) {
      host
        .querySelector(".rv-outlook-main")
        .replaceChildren(
          el(
            "strong",
            "",
            view.kind === "practice" ? `${view.day ?? "—"}` : "—",
          ),
          el(
            "span",
            "",
            view.kind === "practice"
              ? view.preparingColosseum
                ? "Trainingsdag voor Colosseum weekend"
                : "Trainingsdag · geen eindstand"
              : "Fase niet bekend",
          ),
        );
      host.querySelector(".rv-outlook-grid").replaceChildren();
      host.querySelector(".rv-model-note").textContent =
        "Projectie en rangschikking zijn niet beschikbaar in deze fase.";
      if (view.preparingColosseum) {
        const grid = host.querySelector(".rv-outlook-grid");
        for (const [label, value] of [
          ["Training", "3 dagen"],
          ["Colosseum", "4 strijddagen"],
        ]) {
          const cell = el("div");
          cell.append(el("small", "", label), el("strong", "", value));
          grid.append(cell);
        }
        host.querySelector(".rv-model-note").textContent =
          "Vanaf de strijddagen: cumulatieve Colosseum-score en projectie over de resterende dagen. Alle kaarten en tabellen blijven hieronder beschikbaar.";
      }
      profile(entries);
      return;
    }
    const finish = current.finish_outlook || {};
    if (numeric(finish.projected_finish) === null) {
      host.querySelector(".rv-model-note").textContent = cumulative
        ? "Voor een verwachting zijn officiële cumulatieve punten nodig."
        : "Voor een verwachting zijn officiële dagpunten nodig.";
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
      if (button.disabled) return;
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
      setBusy(true);
    },
    error(message) {
      clear(
        message ||
          "Officiële racedata kon niet worden geladen. Probeer opnieuw.",
      );
    },
  };
})();
