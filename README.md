# CW Stats Vercel Deploy

Serverless Python functie en statische pagina om de CW race-statistieken te tonen en snel te kopiëren.

## Lokaal script draaien

```bash
python cwstats_race.py --url https://cwstats.com/clan/9YP8UY/race
```

## Deployen op Vercel (Hobby)
1. Push deze repo naar GitHub.
2. Importeer de repo in Vercel.
3. Open de root (`/`) voor de statische site en `/api/cwstats` voor de JSON output.

Een refresh van de pagina of de Refresh-knop roept de serverless functie opnieuw aan; je laptop hoeft niet aan te staan.
