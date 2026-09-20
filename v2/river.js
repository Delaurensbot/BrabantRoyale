(function () {
  'use strict';
  const host = document.getElementById('riverHero');
  if (!host) return;
  let current = null;
  let mode = 'live';
  const number = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) ? Number(value) : null;
  const format = value => number(value) === null ? '—' : Number(value).toLocaleString('nl-NL', { maximumFractionDigits: 1 });
  const sameTag = (a, b) => String(a || '').replace('#', '').toUpperCase() === String(b || '').replace('#', '').toUpperCase();
  const el = (tag, cls, value) => { const node = document.createElement(tag); if (cls) node.className = cls; if (value !== undefined) node.textContent = value; return node; };
  let boatId = 0;
  const boat = (color, own) => { const id = ++boatId; return `<svg viewBox="0 0 130 150" aria-hidden="true"><defs><linearGradient id="rv-hull-${id}" x2="1" y2=".4"><stop stop-color="#d19b56"/><stop offset=".5" stop-color="#9a6539"/><stop offset="1" stop-color="#563b2c"/></linearGradient></defs><ellipse cx="68" cy="91" rx="37" ry="53" fill="#17777d" opacity=".19"/><path d="M30 111Q20 133 15 143M97 109Q110 132 116 142M41 120L37 148M87 119L94 148" fill="none" stroke="#d7ffff" stroke-width="3" opacity=".65"/><path d="M65 15Q110 50 103 109L89 128L42 128L27 109Q20 50 65 15Z" fill="url(#rv-hull-${id})" stroke="#66452c" stroke-width="2"/><path d="M65 18Q99 48 96 107L84 117L46 117L35 106Q31 48 65 18Z" fill="#e3b679"/><path d="M65 24L65 115M39 63L93 63M35 80L96 80M37 96L94 96M45 111L86 111" stroke="#b5854e" stroke-width="2"/><path d="M34 111L46 124L85 124L99 110" fill="none" stroke="#f3d49b" stroke-width="4"/><path d="M35 85L21 95M98 84L112 94" stroke="#d2a267" stroke-width="5"/><path d="M64 27L64 111" stroke="#634c36" stroke-width="5"/><path d="M31 43Q65 32 98 43L103 84Q66 69 28 85Z" fill="#${color}" stroke="#fff1c2" stroke-width="2"/><path d="M66 38Q73 59 68 77L103 84L98 43Z" fill="#000" opacity=".1"/><path d="M51 53L65 49L78 53L75 68L65 75L55 68Z" fill="#fff3b7"/><path d="M56 57L60 61L65 54L70 61L75 57L72 67L59 67Z" fill="${own ? '#e5ad32' : '#' + color}"/><path d="M65 13L85 17L65 25Z" fill="#${color}" stroke="#fff4ce"/><path d="M48 120L48 129M82 120L82 129" stroke="#65472f" stroke-width="4"/></svg>`; };
  host.innerHTML = `<div class="rv-shell"><nav class="rv-nav" aria-label="V2 navigatie"><a class="rv-brand" href="/v2/"><span class="rv-brand-mark" aria-hidden="true">♜</span><span>BRABANT ROYALE<small>CLAN WAR COMMAND CENTER</small></span></a><div class="rv-nav-right"><span class="rv-version">V2 EXPERIMENT</span><a href="/">Originele site <span aria-hidden="true">↗</span></a></div></nav><div class="rv-intro"><div><div class="rv-eyebrow"><span></span> DE RIVIER WACHT OP NIEMAND</div><h1>Iedere aanval telt.<br><em>Pak de voorsprong.</em></h1><p>Jouw clan. Vier rivalen. Eén overzicht dat laat zien waar je staat — en waar je kunt eindigen.</p></div><aside class="rv-outlook" aria-label="Verwachting voor jouw clan"><div class="rv-outlook-label">JOUW VERWACHTING <span>↗</span></div><div class="rv-outlook-main">—<small>Wachten op officiële data</small></div><div class="rv-outlook-bottom">Berekening op basis van de officiële Clash Royale API.</div></aside></div><div class="rv-race-heading"><div><div class="rv-eyebrow">HET STRIJDTONEEL</div><h2>De river race<span class="rv-live-dot" aria-hidden="true"></span></h2></div><div class="rv-switch" role="group" aria-label="Scoreweergave"><button type="button" data-mode="live" aria-pressed="true">Huidige stand</button><button type="button" data-mode="projection" aria-pressed="false">Verwachting <span aria-hidden="true">↗</span></button></div></div><div class="rv-scene"><div class="rv-shore rv-shore-left" aria-hidden="true"></div><div class="rv-shore rv-shore-right" aria-hidden="true"></div><div class="rv-water-pattern" aria-hidden="true"></div><div class="rv-scene-top"><span class="rv-scope">OFFICIËLE API</span><span class="rv-direction">MEER PUNTEN <span aria-hidden="true">→</span></span></div><div class="rv-lanes"></div><div class="rv-state" role="status"></div><div class="rv-scene-bottom"><span><i></i> JOUW CLAN</span><span>Relatieve score · geen in-game afstand</span></div></div><div class="rv-footnote"><p class="rv-explainer">Officiële scores, helder in beeld.</p><a href="#dashboard">Alle statistieken <span aria-hidden="true">↓</span></a></div></div>`;
  const lanes = host.querySelector('.rv-lanes');
  host.querySelector('[data-mode="projection"]').textContent = 'Projection ↗';
  host.querySelector('.rv-direction').textContent = '↑ MEER PUNTEN';
  const scene = host.querySelector('.rv-scene');
  const viewport = el('div', 'rv-viewport'); viewport.tabIndex = 0; viewport.setAttribute('role', 'region'); viewport.setAttribute('aria-label', 'Rivier met alle clans. Scroll horizontaal om alle boten te zien.');
  scene.before(viewport); viewport.append(scene);
  viewport.after(el('p', 'rv-scroll-hint', '↔ Veeg over de rivier om alle clans te bekijken'));
  const landscape = el('div', 'rv-landscape'); landscape.setAttribute('aria-hidden', 'true');
  landscape.innerHTML = `<svg viewBox="0 0 1200 700" preserveAspectRatio="none"><defs><g id="rv-tree"><ellipse cy="24" rx="24" ry="10" fill="#1b6b5a" opacity=".24"/><path d="M-4 9L-3 31L5 31L6 8" fill="#735435"/><path d="M0-35L-24 4L-13 3L-29 22Q0 33 29 22L14 3L23 4Z" fill="#3c7946"/><path d="M0-35L-7 4L-16 20L1 25L1-26Z" fill="#75a454"/><path d="M1-22L10-4L1-7Z" fill="#a0bd65"/></g><g id="rv-rock"><path d="M-16 0L-6-14L12-9L20 8L4 16L-15 10Z" fill="#a1b09c"/><path d="M-16 0L-6-14L12-9L3 3Z" fill="#d0d1b2"/><path d="M3 3L20 8L4 16Z" fill="#7d968d"/></g></defs><path d="M0 0H115L89 52L106 107L79 168L96 216L67 290L91 348L63 428L89 488L68 567L101 630L92 700H0Z" fill="#d9d2a2"/><path d="M0 0H99L74 52L90 107L65 168L81 216L51 290L74 348L47 428L75 488L53 567L85 630L77 700H0Z" fill="#8bb567"/><path d="M1200 0H1085L1106 74L1081 130L1117 192L1095 275L1121 352L1090 418L1119 491L1093 570L1110 644L1088 700H1200Z" fill="#d9d2a2"/><path d="M1200 0H1101L1122 74L1097 130L1133 192L1111 275L1137 352L1106 418L1135 491L1109 570L1126 644L1104 700H1200Z" fill="#8bb567"/><path d="M470 0H730L720 44L678 68H524L480 40Z" fill="#d6c999"/><path d="M480 0H720L707 32L672 52H533L492 28Z" fill="#90ad64"/><g transform="translate(600 28)"><ellipse cy="32" rx="79" ry="16" fill="#426e5c" opacity=".2"/><path d="M-55-16H55V31H-55Z" fill="#a0b5b0"/><path d="M-48-11H46V18H-48Z" fill="#dbe0c7"/><path d="M-17 32V9Q0-14 17 9V32Z" fill="#405f61"/><path d="M-14 31V11Q0-6 14 11V31Z" fill="#94774b"/><path d="M-67-28H-34V31H-67Z" fill="#bac9bb"/><path d="M34-28H67V31H34Z" fill="#bac9bb"/><path d="M-70-29L-51-55L-31-29Z" fill="#597fbc"/><path d="M31-29L51-55L70-29Z" fill="#597fbc"/><path d="M-51-55L-51-67L-30-62L-51-57M51-55L51-67L71-62L51-57" fill="#eabe56"/><path d="M-55-9H-46V5H-55M46-9H55V5H46" fill="#577678"/><path d="M-32-22H-22V-31H-11V-22H0V-31H11V-22H22V-31H32V-12H-32Z" fill="#e0e3cb"/></g>${[90,190,300,420,530,650].map((y,i)=>`<use href="#rv-tree" transform="translate(${i%2?24:54} ${y}) scale(${i%2?1.1:.9})"/><use href="#rv-tree" transform="translate(${i%2?1164:1144} ${y+22})"/><use href="#rv-rock" transform="translate(${i%2?68:37} ${y+48}) scale(.7)"/><use href="#rv-rock" transform="translate(1135 ${y+65}) scale(.6)"/>`).join('')}</svg>`;
  landscape.querySelector('g[transform]').setAttribute('transform', 'translate(600 67) scale(.8)');
  const banks = landscape.querySelectorAll('svg > path');
  banks[4].setAttribute('d', 'M470 0H730L720 74L678 106H524L480 74Z');
  banks[5].setAttribute('d', 'M480 0H720L707 64L672 91H533L492 64Z');
  scene.prepend(landscape);
  const state = host.querySelector('.rv-state');
  const outlook = host.querySelector('.rv-outlook-main');
  const bottom = host.querySelector('.rv-outlook-bottom');
  const colors = ['318bfa', 'e87371', '9766d8', 'efb94f', '60b89a', 'b48b73'];
  function clear(message) {
    current = null; lanes.replaceChildren(); state.textContent = message; state.hidden = false;
    outlook.replaceChildren(el('span', '', '—'), el('small', '', 'Nog geen verwachting beschikbaar'));
    bottom.textContent = 'Berekening op basis van de officiële Clash Royale API.';
    host.querySelector('.rv-scope').textContent = 'OFFICIËLE API';
    host.querySelector('.rv-explainer').textContent = 'Officiële scores, helder in beeld.';
  }
  function render() {
    if (!current) return;
    const rows = Array.isArray(current.overview_rows) ? current.overview_rows.filter(row => row && typeof row === 'object') : [];
    if (!rows.length) { clear('Er is nog geen officiële race-informatie beschikbaar voor deze clan.'); return; }
    state.hidden = true;
    const field = mode === 'live' ? 'medals' : 'projected_medals';
    const score = row => row.score_available === false ? null : number(row[field]);
    const ranked = [...rows].sort((a, b) => (score(b) ?? -1) - (score(a) ?? -1));
    const max = Math.max(1, ...rows.flatMap(row => [number(row.medals) ?? 0, number(row.projected_medals) ?? 0]));
    lanes.style.setProperty('--clans', String(rows.length));
    scene.style.setProperty('--scene-width', `${Math.max(820, rows.length * 150 + 120)}px`);
    const cumulative = rows.some(row => row.score_scope === 'colosseum_cumulative');
    host.querySelector('.rv-scope').textContent = cumulative ? 'COLOSSEUM · CUMULATIEVE SCORE' : 'RIVER RACE · DAGSCORE';
    host.querySelector('.rv-explainer').textContent = mode === 'live' ? 'Bron: officiële Clash Royale API. Boten vergelijken ' + (cumulative ? 'cumulatieve Colosseum-punten.' : 'de dagscore.') : 'Lokale schatting uit officiële API-data: huidige score + resterende decks × gemiddeld punten per deck. ' + (cumulative ? 'Inclusief resterende Colosseum-dagen. ' : 'Verwachte score voor deze racedag. ') + 'Geen gegarandeerde eindstand.';
    const existing = new Map([...lanes.children].map(node => [node.dataset.key, node]));
    rows.forEach((row, index) => {
      const key = String(row.tag || index);
      let lane = existing.get(key);
      const own = sameTag(row.tag, current.clan_tag);
      if (!lane) {
        lane = el('div', 'rv-lane'); lane.dataset.key = key;
        const clan = el('div', 'rv-clan'); clan.append(el('span', 'rv-rank'), el('div', 'rv-clan-text'));
        const track = el('div', 'rv-track'); const vessel = el('div', 'rv-vessel');
        const drawing = el('div', 'rv-boat'); drawing.innerHTML = boat(own ? colors[0] : colors[(index % (colors.length - 1)) + 1], own);
        const metrics = el('div', 'rv-metrics'); metrics.append(el('strong', 'rv-score'), el('span', 'rv-attacks'), el('span', 'rv-average'));
        vessel.append(drawing, clan, metrics); track.append(vessel); lane.append(track); lanes.append(lane);
      }
      existing.delete(key); lane.classList.toggle('rv-own', own);
      const value = score(row); lane.querySelector('.rv-rank').textContent = value === null ? '—' : String(ranked.indexOf(row) + 1).padStart(2, '0');
      lane.querySelector('.rv-clan-text').replaceChildren(el('strong', '', row.name || 'Onbekende clan'), el('small', '', own ? 'JOUW CLAN' : row.tag || 'RIVAAL'));
      lane.querySelector('.rv-score').textContent = `${format(value)} ${mode === 'projection' ? 'verw. ' : ''}punten`;
      lane.querySelector('.rv-attacks').textContent = `${format(row.decks_used_today)} / ${format(row.decks_total_today)} aanvallen`;
      lane.querySelector('.rv-average').textContent = `${format(row.avg_medals_per_deck)} pnt / deck`;
      requestAnimationFrame(() => lane.style.setProperty('--progress', value === null ? '0' : String(value / max)));
    });
    existing.forEach(node => node.remove());
    const finish = current.finish_outlook || {};
    const rank = number(finish.projected_rank);
    outlook.replaceChildren(el('span', '', rank === null ? '—' : `#${rank}`), el('small', '', rank === null ? 'Onvoldoende data voor een verwachting' : `${format(finish.projected_finish)} verwachte punten`));
    bottom.textContent = `Beste #${format(finish.best_rank)} · Laagste #${format(finish.worst_rank)} · ${format(finish.battles_left)} aanvallen over`;
  }
  host.querySelectorAll('[data-mode]').forEach(button => button.addEventListener('click', () => {
    mode = button.dataset.mode;
    host.querySelectorAll('[data-mode]').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
    render();
  }));
  window.RiverV2 = {
    update(data) { current = data && typeof data === 'object' ? data : {}; render(); },
    loading(clanTag) { clear(`Officiële racedata ophalen${clanTag ? ' voor ' + clanTag : ''}…`); },
    error(message) { clear(message || 'De officiële racedata kon niet worden geladen. Probeer het opnieuw.'); }
  };
  clear('Officiële racedata ophalen…');
})();
