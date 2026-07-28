# Clan War Stats on Vercel

Deze repo draait een simpele statische pagina met een Python Serverless Function op Vercel. De frontend haalt data op via `/api/cwstats`; de Refresh-knop runt het RoyaleAPI-scrapescript opnieuw en toont de nieuwste tekst.

## Belangrijk
- `/api/cwstats` bestaat en geeft JSON terug met `ok: true` wanneer het scrapen/parsen lukt (data komt nu van RoyaleAPI).
- De website toont drie blokken (Race, Clan Stats, Battles left) en de kopieerknoppen werken per blok én via klik op de tekst.
- Lokaal openen via `file://` werkt niet, omdat `/api/cwstats` dan niet bestaat.

## Deploy
1. Push de main branch naar GitHub.
2. Ga in Vercel naar **New Project** en importeer de repo.
3. Kies framework **Other** en laat het build-commando leeg (niet nodig).
4. Deploy.
5. Test in de browser:
   - `https://<project>.vercel.app/api/cwstats` moet JSON tonen.
   - `https://<project>.vercel.app` moet de pagina tonen en data ophalen.


## Temporary clan member test route
- API route: `/api/test-clan` (server-side Vercel Python function).
- Temporary page: `/test-clan.html`.
- Required environment variable: `CLASH_ROYALE_API_KEY` (set in Vercel project settings).

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
