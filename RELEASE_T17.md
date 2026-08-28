# T17 release, rollback en monitoring

Dit document beschrijft de gecontroleerde afronding van de tijdelijke migratie.
Het bevat geen secretwaarden. Voeg productie-uitkomsten alleen als
samenvattingen toe; plak geen response-body's, tokens of leader-only gegevens
in git.

## Release record

- Scope: T17 legacy cleanup en productie-release.
- T16 baseline: `3bc879d63c0cc574fbdd66357b34d589defb4266`.
- Pre-migration rollback-tag: `brabant-royale-pre-migration`.
- Verwachte tag-target: `d979fcc5ca165102e7f147d6833214f8f5ca7107`.
- T17 release candidate: `7cbe7d58dd781c570734f202536496ea5ca4893c`.
- Productiedeploy: `geblokkeerd`; de lokale Vercel-CLI ontbrak en
  `npx vercel whoami` accepteerde geen geldige token. De claimable-preview
  fallback is niet gebruikt als vervanging voor productie.

## Productiebronnen en veiligheidsgrenzen

De primaire productiebron is de officiële Clash Royale API via
`api/clash_client.py`. De routes gebruiken geen HTML-scraping, oude
prototypepagina of succesvolle fallback naar een tweede bron. Ontbrekende of
stale upstream-data blijft als zodanig gemarkeerd.

De publieke routes mogen geen API-key, Supabase secret/service-role key,
monitor-secret, admin-key, Discord-webhook of leader-only gegevens aan de
browser teruggeven. `/api/war_monitor` blijft server-to-server en gebruikt de
normale ingest-authenticatie.

## Environment variables

Zet waarden alleen in het Vercel-project of de relevante secret store; zet geen
waarden in deze repository.

| Variabele | Gebruik | Scope |
|---|---|---|
| `CLASH_ROYALE_API_KEY` | officiële Clash Royale API | server-only |
| `SUPABASE_URL` | optionele historische opslag | server/config |
| `SUPABASE_PUBLISHABLE_KEY` | Supabase publieke client/config | public-safe volgens Supabase-configuratie |
| `SUPABASE_SECRET_KEY` | historische opslagbeheer | server-only |
| `SUPABASE_SERVICE_ROLE_KEY` | compatibele server-side Supabase-auth | server-only |
| `SUPABASE_INGEST_TOKEN` | snapshot-ingest | server-only / GitHub secret |
| `WAR_MONITOR_SECRET` | geauthenticeerde monitor-POST | server-only |
| `WAR_STATUS_LEADER_SECRET` | leader-only war-statusacties | server-only |
| `DISCORD_WAR_WEBHOOK_URL` | optionele Discord-notificatie | server-only |
| `WAR_MONITOR_NOTIFICATION_POLICY` | notificatiebeleid | server/config |

## Lokale release gate

Voer uit vanaf de gecontroleerde T17-commit:

```text
python -m pytest -q
python -m compileall -q api supabase_history.py
ruff check <alle gewijzigde Python-bestanden en tests>
git diff --check
git status --short --branch
```

Controleer daarnaast de diff tegen de T16-baseline, de toegestane
bestandslijst, secret/frontend/scraping/prototype-scans en de exacte target van
`brabant-royale-pre-migration`.

## Deploy

1. Controleer dat de worktree schoon is en dat de release-commit exact de
   gecontroleerde T17-diff bevat.
2. Controleer `brabant-royale-pre-migration` tegen het verwachte target.
3. Deploy alleen die commit via de gekoppelde Vercel-workflow/CLI. Toon geen
   CLI-output met tokens en voer geen repository- of database-reset uit.
4. Noteer de Vercel-deploymentstatus en controleer daarna de smoke-tests.
5. Bij ontbrekende credentials of een niet-verifieerbare projectkoppeling:
   stop de deploy, laat productie ongemoeid en noteer exact welke controle
   niet kon worden uitgevoerd zonder credentialwaarden te vermelden.

## Productie smoke tests

Gebruik de productie-URL en een syntactisch geldige testtag voor scouting. Noteer
per route alleen de status en geselecteerde metadata, niet de volledige JSON.

| Methode en route | Minimale controle |
|---|---|
| `GET /api/cwstats?clan=9YP8UY` | HTTP-status, `ok`, `source`, `fetched_at`, `data_quality`, race phase, participant count, snapshot freshness en event counts waar aanwezig |
| `GET /api/analytics?clan=9YP8UY` | HTTP-status, `ok`, `source`, `fetched_at`, `data_quality`, snapshot freshness en event counts |
| `GET /api/scouting?tag=<testtag>&clan=9YP8UY` | HTTP-status, `ok`, `source`, `fetched_at`, `data_quality` en fout/participant- of profielstatus zonder privégegevens |
| `GET /api/war_status?clan=9YP8UY` | HTTP-status, `ok`, `source`, `fetched_at`, `data_quality`, race phase, participant count, snapshot freshness en event counts |
| `POST /api/war_monitor` | HTTP-status en auth/`ok`-uitkomst; alleen uitvoeren met de bestaande monitor-secret, nooit een secret loggen |

Een ontbrekende monitor-secret betekent dat de geauthenticeerde POST niet veilig
kan worden uitgevoerd. Een eventueel gecontroleerde `401` zonder credential is
alleen een auth-guard-check, geen geslaagde monitor-smoketest.

## Monitoring

De bestaande scheduled monitor-workflow roept `/api/war_monitor` periodiek aan.
Controleer in de workflow-uitkomst:

- HTTP-status en `ok`;
- `source` moet officieel blijven;
- `fetched_at` en snapshot freshness mogen niet onverwacht verouderen;
- `data_quality`, race phase en participant count moeten aanwezig en plausibel
  zijn;
- event counts en Discord-notificaties mogen alleen veranderen door normale
  monitorruns.

Bij herhaalde upstream-fouten: bewaar de expliciete fout/stale-status, controleer
de Clash Royale API-health en credentials, en voorkom handmatige nulwaarden.
Wijzig geen productie-data buiten normale snapshot- of monitorflows.

## Rollback

De tag `brabant-royale-pre-migration` blijft intact op
`d979fcc5ca165102e7f147d6833214f8f5ca7107`. Dat is de één-staps
pre-migrationbaseline:

1. stop of pauzeer de releaseworkflow;
2. verifieer lokaal opnieuw dat de tag naar de verwachte hash wijst;
3. deploy exact de commit achter die tag via Vercel;
4. voer de beperkte route-smoke-tests opnieuw uit;
5. laat Supabase-snapshots staan; rollback verwijdert of herschrijft geen
   productie-data;
6. documenteer oorzaak, deployment-id en herstelstatus zonder secrets.

Een rollback herstelt de codebaseline, niet verdwenen upstream-history. Na
herstel moet de monitorprocedure opnieuw worden geactiveerd en gecontroleerd.

## T17 verificatierecord (2026-08-28)

De lokale release gate op de release candidate was volledig groen:

- `python -m pytest -q`: 315 passed;
- `python -m compileall -q api supabase_history.py`: geslaagd;
- Ruff op alle gewijzigde Python-bestanden en tests: geslaagd;
- `git diff --check`: geslaagd;
- legacy-, frontend-secret- en unauthorized-files-scan: geslaagd;
- `brabant-royale-pre-migration` wees naar de verwachte hash.

Een post-T17 productie-smoke-test kon niet worden uitgevoerd omdat de
productiedeploy veilig is gestopt op ontbrekende/ongeldige Vercel-auth. De
volgende read-only baselinecontrole op de bestaande productie-URL is wel
uitgevoerd; deze bewijst niet dat T17 live staat:

| Methode en route | Baseline-uitkomst vóór T17-deploy |
|---|---|
| `GET /api/cwstats?clan=9YP8UY` | HTTP 200, JSON, `ok: true`; de oudere live-respons bevatte de T17-metadata (`source`, `fetched_at`, `data_quality`, race/freshness/eventvelden) niet betrouwbaar |
| `GET /api/analytics?clan=9YP8UY` | HTTP 200, JSON, `ok: true`; oudere live-respons bevatte geen gestandaardiseerde T17-bron/freshnessvelden |
| `GET /api/scouting?tag=%23PLAYER1&clan=9YP8UY` | HTTP 404; route niet aanwezig in de bestaande live deployment, dus geen T17-resultaat |
| `GET /api/war_status?clan=9YP8UY` | HTTP 404; route niet aanwezig in de bestaande live deployment, dus geen T17-resultaat |
| `POST /api/war_monitor` zonder credential | HTTP 404; route niet aanwezig, daarom is alleen geen data-mutatie uitgevoerd en is de auth-guard niet live getest |

Er zijn geen response-body’s, playerdetails, leadergegevens, tokens of andere
secrets gelogd. Er is geen productie-data gewijzigd. Na herstel van Vercel-auth
moeten alle vijf post-release smoke-tests opnieuw worden uitgevoerd en moet
dit record worden aangevuld met de echte deploymentstatus en geselecteerde
metadata.
