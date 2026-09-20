// Browser-only fixture: pipe into agent-browser eval --stdin on a local V2 page.
// Never loaded by the deployed site. Exercises the real fetch/render pipeline.
(async () => {
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const names = ['Brabant Royale', 'Royal Guardians', 'The Last Kingdom', 'Les Mousquetaires', 'Nordic Vikings'];
  const tags = ['#9YP8UY', '#AAA', '#BBB', '#CCC', '#DDD'];
  const scores = [18200, 20100, 17000, 14900, 13100];
  const projections = [33800, 30400, 29100, 27000, 25400];
  const rows = names.map((name, i) => ({ name, tag: tags[i], medals: scores[i],
    boat_points: scores[i] * 2, decks_used_today: 100 + i * 8, decks_total_today: 200,
    avg_medals_per_deck: 182 - i * 9, projected_medals: projections[i],
    score_available: true, score_scope: 'river_race_day' }));
  const data = { ok: true, clan_tag: '9YP8UY', clan: {name: names[0]}, overview_rows: rows,
    players: [], race_state: {is_colosseum_weekend: false, battle_day: 3},
    finish_outlook: {projected_rank: 1, projected_finish: 33800, best_rank: 1,
      best_finish: 38000, worst_rank: 3, worst_finish: 27000, battles_left: 100,
      duels_left: 12, total_players_participated: 30, projection_scope: 'river_race_day'} };
  window.__v2Fixture = data;
  window.fetch = async url => ({ok: true, json: async () => String(url).includes('test-clan-prototype') ? data : {ok: true}});
  await fetchData();
  assert(document.querySelectorAll('.rv-lane').length === 5, 'Five boats must render');
  assert(document.querySelector('.rv-attacks').textContent.includes('100'), 'Attack usage missing');
  assert(document.querySelector('.rv-average').textContent.includes('182'), 'Average missing');
  assert(document.getElementById('overview').textContent.includes('Brabant Royale'), 'Existing overview missing');
  assert(document.querySelectorAll('#dashboard .card').length === 11, 'Legacy cards not preserved');
  document.querySelector('[data-mode="projection"]').click();
  assert(document.querySelector('.rv-score').textContent.includes('33.800'), 'Projection did not update');
  document.querySelector('[data-mode="live"]').click();
  assert(document.querySelector('.rv-score').textContent.includes('18.200'), 'Live did not restore');
  RiverV2.update({...data, overview_rows: [{...rows[0], medals: null, projected_medals: null, avg_medals_per_deck: null, score_available: false}]});
  assert(document.querySelector('.rv-score').textContent.includes('—'), 'Missing score displayed as zero');
  RiverV2.loading('GPCLVLPP');
  assert(!document.querySelector('.rv-lane'), 'Stale boats after clan switch');
  RiverV2.error('Fixture error');
  assert(document.querySelector('.rv-state').textContent === 'Fixture error', 'Error not shown');
  RiverV2.update({...data, race_state: {is_colosseum_weekend: true}, overview_rows: rows.map(row => ({...row, score_scope: 'colosseum_cumulative'}))});
  assert(document.querySelector('.rv-scope').textContent.includes('CUMULATIEVE'), 'Colosseum scope missing');
  RiverV2.update(data);
  return 'PASS: data integration, 5 boats, attacks, average, retained dashboard, projection/live, missing values, clan clearing, error and Colosseum';
})();
