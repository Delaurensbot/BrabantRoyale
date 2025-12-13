# Brabant Royale CW Stats

Serverless CW stats scraper + static UI for Vercel Hobby. The Python function fetches cwstats.com on each request and the static page shows three copyable blocks (Race, Clan Stats, Battles left).

## Lokaal testen

Voer het scraper-script direct uit:

```bash
python cwstats_race.py --url https://cwstats.com/clan/9YP8UY/race
```

## Deploy op Vercel

1. Push de repository naar GitHub.
2. Importeer het project in Vercel.
3. Open `/` (static site) en `/api/cwstats` (serverless endpoint).

Een refresh in de browser of de Refresh-knop triggert opnieuw het serverless script; je laptop hoeft niet aan te staan.
