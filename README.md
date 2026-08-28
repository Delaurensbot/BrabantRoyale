# Clan War Stats on Vercel

Deze repo draait een statische pagina met Python Serverless Functions op Vercel.
De productie-routes lezen uitsluitend via de server-side officiële Clash Royale
API-client in `api/clash_client.py`. De tijdelijke HTML-bron en prototypepagina
zijn verwijderd; Supabase wordt alleen gebruikt voor de eigen historische
snapshots.

## Belangrijk

- `/api/cwstats`, `/api/analytics`, `/api/scouting` en `/api/war_status` geven
  officiële API-data met bron-, freshness- en kwaliteitsmetadata.
- Een upstream-storing wordt niet als een nulprestatie gepresenteerd: routes
  geven een expliciete fout-, partiële of stale-status terug.
- `/api/war_monitor` is een server-to-server POST-route en vereist de ingest-
  secret; secrets komen nooit in HTML, client-JavaScript of logs.
- Lokaal openen via `file://` werkt niet, omdat de `/api/*`-routes dan niet
  bestaan.

## Deploy

Deploy uitsluitend een gecontroleerde commit met de aanwezige Vercel-workflow.
De volledige release-, smoke-test-, monitoring- en rollbackprocedure staat in
[RELEASE_T17.md](RELEASE_T17.md). Controleer vóór release altijd dat de
rollback-tag `brabant-royale-pre-migration` intact is.

De productie-URL is `https://brabant-royale.vercel.app` wanneer het gekoppelde
Vercel-project die standaardnaam gebruikt. Test na een deploy minimaal de
routes en velden die in [RELEASE_T17.md](RELEASE_T17.md) staan.

## Langdurige analytics met Supabase

De Clash Royale API levert maar een beperkt aantal recente river races. Daarom
slaat `.github/workflows/snapshot-clan-history.yml` elke maandag om 13:00
Nederlandse tijd de beschikbare weken op in Supabase. De job verwerkt standaard
alle drie geconfigureerde Brabant Royale-clans en gebruikt een idempotente
upsert, zodat handmatig opnieuw draaien geen dubbele regels maakt.

### 1. Database aanmaken

Pas de migratie in `supabase/migrations` toe op het bedoelde Supabase-project:

```bash
npx supabase@latest link --project-ref <project-ref>
npx supabase@latest db push
```

De tabel heeft RLS ingeschakeld. `anon` en `authenticated` hebben alleen
leesrecht; alleen een Supabase secret key kan snapshots schrijven.

### 2. GitHub Actions secret

Voeg onder **Repository settings > Secrets and variables > Actions** toe:

- `SUPABASE_INGEST_TOKEN`

De workflow kan daarna eenmalig handmatig worden gestart via **Actions >
Snapshot clan analytics history > Run workflow**. Dat vult direct de weken die
de Clash API op dat moment nog teruggeeft. Weken die vóór deze koppeling al uit
de API-historie zijn verdwenen kunnen niet achteraf worden hersteld; vanaf de
eerste succesvolle run groeit de historie iedere week verder.

De workflow roept de beveiligde Vercel-route `/api/snapshot_history` aan. De
route gebruikt de bestaande `CLASH_ROYALE_API_KEY` in Vercel en kan met het
ingest-token uitsluitend de clan-historytabel invoegen of bijwerken. Er is dus
geen databasebrede Supabase secret key in GitHub nodig.

### 3. Vercel environment variables

`SUPABASE_URL` en `SUPABASE_PUBLISHABLE_KEY` mogen optioneel in Vercel worden
ingesteld. De repository bevat veilige publieke defaults voor het
BrabantRoyale-project, zodat de eerstvolgende deployment direct kan lezen en
schrijven. `CLASH_ROYALE_API_KEY` blijft uitsluitend een geheime Vercel-variable.

Na een nieuwe deploy combineert `/api/analytics` de live Clash-data met alle
opgeslagen Supabase-weken. Zonder deze variabelen blijft de endpoint werken met
de publieke projectdefaults.

### Weekuitzonderingen en beheer

Een clanleider kan in de spelerdetails een specifieke week wel of niet laten
meetellen. Uitzonderingen worden gedeeld opgeslagen in
`public.clan_war_week_exclusions` en gelden voor alle analytics:

- MVP, reliability en tabeltotalen
- promotie met een venster van 2, 4 of 6 weken
- degradatieadvies bij meer dan 2 gemiste aanvallen in maximaal 10 weken
- clan-fit screening

De beheerkey wordt alleen voor de browsersessie bewaard en via
`X-Analytics-Admin-Key` naar `/api/analytics_overrides` gestuurd. De database
controleert uitsluitend een SHA-256-hash via RLS. De key geeft geen algemene
database- of Supabase-beheerrechten en er staat nooit een Supabase secret key in
de frontend.

### Configureerbaar clanbeleid en leidersbesluiten (T13)

T13 voegt `public.clan_policy_settings` en het admin-only auditlog
`public.leader_decisions` toe. Beleid is server-side leesbaar via
`GET /api/clan_policy?clan=<CLAN_TAG>` en schrijfbaar met het bestaande
`X-Analytics-Admin-Key` via `POST`, `PUT` of `PATCH /api/clan_policy`.
Leidersbesluiten worden uitsluitend via `/api/leader_decisions` met diezelfde
admin-key gelezen of toegevoegd; actor, reden en individuele besluiten komen
niet in publieke responses. Defaults, bounds, responsevorm en aannames staan
in [`CLAN_POLICY_CONTRACT.md`](CLAN_POLICY_CONTRACT.md).

### Nieuwe spelers screenen

`/api/scouting?tag=<PLAYER_TAG>&clan=<CLAN_TAG>` haalt een officieel
spelersprofiel op en combineert dit, wanneer beschikbaar, met onze opgeslagen
war-historie. Profieldata is alleen een voorselectie; externe spelers houden
altijd een proefperiodeadvies totdat minimaal twee eigen war-weken zijn
waargenomen. De berekening en beperkingen staan in
`SCOUTING_FRAMEWORK_REPORT.md`.

## Live war monitor

`.github/workflows/live-war-monitor.yml` roept de beveiligde productie-route
`POST /api/war_monitor` iedere vijf minuten aan en kan ook handmatig worden
gestart. Stel onder **Repository settings > Secrets and variables > Actions**
het secret `WAR_MONITOR_SECRET` in met dezelfde waarde als de Vercel-variable
voor T08. De waarde staat niet in de repository en wordt alleen als
`X-War-Monitor-Secret`-header verstuurd.

De workflow gebruikt standaard
`https://brabant-royale.vercel.app/api/war_monitor`. Een andere productie-URL
kan veilig als niet-geheime Actions-repositoryvariable `WAR_MONITOR_URL` worden
ingesteld. GitHub Actions schedules zijn best-effort en kunnen vertraagd
starten; `*/5` is de minimale praktische intervalgrens van het platform, geen
garantie op exacte uitvoering. Vercel Hobby past niet bij deze frequentie.
Geplande en handmatige monitorruns worden geserialiseerd en een lopende run
wordt niet geannuleerd. De bestaande maandagworkflow
`.github/workflows/snapshot-clan-history.yml` blijft de weekhistory opslaan.
