# Projectnorm Brabant Royale

Volg deze afspraken bij iedere Codex-taak in deze repository.

## Startcontrole

- Begin met `git status --short --branch` en meld bestaande lokale wijzigingen voordat bestanden worden aangepast.
- Werk alleen aan bestanden die binnen de gevraagde taak vallen en revert geen wijzigingen van de gebruiker.
- Gebruik voor nieuw werk standaard een branch met prefix `codex/`; werk niet direct op `main` zonder expliciete opdracht.

## Controle en versiebeheer

- Controleer de diff met `git diff --check` en draai de relevante tests voordat je commit.
- Commit alleen taakgerelateerde bestanden. Commit nooit secrets, `.env`-bestanden, lokale caches of exports.
- Push een reviewbare branch en maak of update een GitHub pull request zodat Vercel een Preview Deployment kan bouwen.
- Vermeld in de pull request wat is gewijzigd, welke checks zijn uitgevoerd en wat nog niet lokaal kon worden geverifieerd.
- Merge de pull request niet en voer geen productie-deployment uit zonder expliciete goedkeuring van de gebruiker.

## Databronnen

- Houd officiële Clash Royale API-data en lokaal afgeleide berekeningen herkenbaar gescheiden van scraperdata.
- Laat een onderdeel dat expliciet op de officiële API hoort te draaien niet stil terugvallen op scraperdata.
