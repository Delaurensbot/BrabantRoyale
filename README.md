# CW Stats op Vercel

Een minimale static site + serverless Python endpoint die de CW race/statistieken ophaalt van cwstats.com en toont op een copy-vriendelijke pagina.

## Lokaal het script draaien

```bash
python cwstats_race.py --url https://cwstats.com/clan/9YP8UY/race
```

## Deploy naar Vercel (Hobby)
1. Push de code naar GitHub.
2. Importeer de repo in Vercel.
3. Open `/` voor de website en `/api/cwstats` voor de JSON API.

Een refresh (of de Refresh-knop op de pagina) triggert telkens de serverless functie; je laptop hoeft dus niet aan te staan.
