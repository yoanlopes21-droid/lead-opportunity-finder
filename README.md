# Lead Opportunity Finder

Application locale de qualification de leads commerciaux pour le recrutement dans le Val-de-Marne (94).

## État de ce socle

Le projet comprend une API FastAPI, une base SQLite locale, une interface React/Vite minimale et les modèles de données initiaux. Aucun moteur de recherche, connecteur externe, enrichissement ou contact automatique n'est inclus.

## Démarrage

Voir les instructions détaillées dans `docs/development.md`.

## Principes V1

- Données, preuves et analyses restent séparées.
- Aucun envoi automatique ni intégration Nicoka.
- Les sources sont indépendantes et optionnelles.
- Les clés éventuelles sont lues depuis l'environnement local, jamais depuis le code ou Git.
