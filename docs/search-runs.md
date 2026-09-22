# Runs de recherche commerciale

Une actualisation France Travail ne déclenche aucun enrichissement web. Un run de recherche est une action commerciale séparée, persistée dans `search_runs` et `search_run_items`.

`POST /api/v1/search-runs` crée un run (département `94`, objectif 25, cap Brave 40 par défaut) et lance son traitement en arrière-plan. Le moteur ordonne les opportunités locales par score commercial déterministe, exclut les clients/prospects/exclusions manuelles, puis traite une entreprise à la fois.

Avant tout enrichissement, il recalcule la stratégie de contact à partir des faits durables. Un lead ne compte que s'il est éligible, possède une offre active, un canal préféré fiable avec sa propre provenance, et une pertinence géographique suffisante pour l'opportunité française. L'authenticité d'une coordonnée reste distincte de sa pertinence commerciale : un canal étranger peut rester visible et sourcé sans compter comme actionnable. Un canal national de recrutement explicitement valable pour la France reste admissible avec un avertissement de portée.

Les faits déjà actionnables sont réutilisés sans réseau. Avec un cap Brave à zéro, `official_web` n'est lancé que lorsqu'un signal web durable existe déjà (site vérifié, candidat URL, ou URL issue d'une offre). Un site connu peut ainsi être relu selon les TTL existants, tandis qu'une cible sans seed est écartée rapidement. Avec un budget Brave positif, la découverte reste disponible. Societe.com n'est ni sélectionné ni appelé.

Les endpoints sont :

- `GET /api/v1/search-runs/{id}` pour la progression et `GET /api/v1/search-runs/{id}/events` pour SSE ;
- `POST /api/v1/search-runs/{id}/stop` pour demander un arrêt coopératif ;
- `POST /api/v1/search-runs/{id}/resume` pour reprendre les items non terminés ;
- `GET /api/v1/search-runs/{id}/results` pour les leads actionnables recomposés depuis les données actuelles.

La progression expose le nom d'entreprise lisible et une `completion_reason` structurée. Une fin après parcours complet utilise `candidates_exhausted`, même si le cap Brave vaut zéro ; `target_reached`, `stopped` et `failed` décrivent les autres fins actuellement produites. Les timestamps de progression sont normalisés en UTC avec fuseau explicite.

Le ledger Brave existant reste l'autorité de budget mensuel et de cap par run. Un cap à zéro désactive explicitement Brave. L'âge affiché des offres est calculé depuis leurs dates ; il ne fait pas partie de l'identité ou du cache web. Une nouvelle offre pour une entreprise connue conserve donc les contacts/sites encore frais.
