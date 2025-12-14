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
