# Runs de recherche commerciale

Une actualisation France Travail ne déclenche aucun enrichissement web. Un run de recherche est une action commerciale séparée, persistée dans `search_runs` et `search_run_items`.

`POST /api/v1/search-runs` crée un run (département `94`, objectif 25, cap Brave 40 par défaut) et lance son traitement en arrière-plan. Le moteur ordonne les opportunités locales par score commercial déterministe, exclut les clients/prospects/exclusions manuelles, puis traite une entreprise à la fois.

Avant tout enrichissement, il recalcule la stratégie de contact à partir des faits durables. Un lead ne compte que s'il est éligible, possède une offre active, un canal préféré autre que `none`, et une provenance de contact. Les faits déjà actionnables sont réutilisés sans Brave. Sinon, seul l'adaptateur `official_web` est utilisé ; Societe.com n'est ni sélectionné ni appelé.

Les endpoints sont :

- `GET /api/v1/search-runs/{id}` pour la progression et `GET /api/v1/search-runs/{id}/events` pour SSE ;
- `POST /api/v1/search-runs/{id}/stop` pour demander un arrêt coopératif ;
- `POST /api/v1/search-runs/{id}/resume` pour reprendre les items non terminés ;
- `GET /api/v1/search-runs/{id}/results` pour les leads actionnables recomposés depuis les données actuelles.

Le ledger Brave existant reste l'autorité de budget mensuel et de cap par run. Un cap à zéro désactive explicitement Brave. L'âge affiché des offres est calculé depuis leurs dates ; il ne fait pas partie de l'identité ou du cache web. Une nouvelle offre pour une entreprise connue conserve donc les contacts/sites encore frais.
