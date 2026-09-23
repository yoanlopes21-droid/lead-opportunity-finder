# Lead Opportunity Finder

Application locale de qualification de leads commerciaux pour le recrutement dans le Val-de-Marne (94).

## État de ce socle

Le projet comprend une API FastAPI, une base SQLite locale et une interface React/Vite. Les offres France Travail, les boards employeurs publics Greenhouse/Lever et la découverte Open Web bornée peuvent être lancés manuellement. Les résultats web incomplets restent dans une file de signaux à vérifier. Aucun contact automatique n'est inclus.

## Démarrage

Voir les instructions détaillées dans `docs/development.md`.

## Principes V1

- Données, preuves et analyses restent séparées.
- Aucun envoi automatique ni intégration Nicoka.
- Les sources sont indépendantes et optionnelles.
- Les clés éventuelles sont lues depuis l'environnement local, jamais depuis le code ou Git.
