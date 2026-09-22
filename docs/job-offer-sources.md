# Sources d'offres et découverte web

Audit vérifié le 22 septembre 2026. Ce document distingue strictement les API de
publication ATS des API de lecture d'un catalogue public. Une page visible dans un
navigateur n'autorise pas, à elle seule, une collecte automatisée.

## Statuts

| Source | Statut | Accès retenu | Données et géographie | Limites / coût / authentification |
|---|---|---|---|---|
| France Travail | intégré | API officielle Offres d'emploi v2 | identifiant, titre, entreprise si publiée, lieu, commune/département, contrat, salaire éventuel, dates, URL; filtre département 94 et pagination | OAuth partenaire; quota et limites de fenêtre gérés par le collecteur |
| Sites carrière Greenhouse | intégré, borné par employeur | API Job Board publique en lecture | identifiant, titre, lieu, description, date de mise à jour, URL; seules les localisations explicitement rattachables au 94 sont conservées | GET public sans authentification; un `board_token` et un nom d'employeur doivent être configurés explicitement; pas de recherche transverse des employeurs |
| HelloWork | discovery-only | résultats Brave pointant vers des pages d'offre publiques | les pages d'offre indexées peuvent exposer titre, entreprise, lieu, contrat, salaire éventuel, date, référence et URL | aucune API publique documentée de lecture du catalogue; l'intégration ATS sert à publier/synchroniser; `robots.txt` interdit les routes de recherche, API et URLs à paramètres aux robots génériques; pas de collecteur direct |
| Welcome to the Jungle | accès partenaire requis / discovery-only | résultats Brave; provider direct futur si un accord est obtenu | l'API ATS documente des offres, bureaux, contrat, salaire, dates et URLs | token obligatoire; `jobs_r` lit les offres de ses propres organisations; les scopes `su_jobs_r` inter-entreprises requièrent un partenariat; ce n'est pas une API publique de catalogue |
| LinkedIn Jobs | discovery-only | URL et extrait publiquement indexés par Brave uniquement | source, URL et extrait; promotion en offre structurée seulement après preuve séparée de l'entreprise, du rôle et d'une localisation 94 | aucun accès direct; `robots.txt` interdit l'accès automatisé sans permission expresse; l'API Jobs documentée publie des offres pour des partenaires, elle ne lit pas le catalogue public |
| Indeed | discovery-only | URL et extrait publiquement indexés par Brave uniquement | source, URL et extrait; mêmes critères stricts de promotion qu'une autre découverte web | aucune API publique de recherche catalogue identifiée; Job Sync est une API partenaire ATS de création/gestion des publications; aucun scraping direct |
| Leboncoin Emploi | discovery-only | résultats Brave ciblés | source, URL et extrait, éventuellement candidat à vérifier | `robots.txt` indique que les robots/méthodes automatiques sont interdits sans permission spéciale et bloque API/recherche; aucun appel direct |
| Apec | accès partenaire requis / discovery-only | résultats Brave; partenariat futur possible | les pages publiques et sitemaps sont indexables; le flux peut fournir des extraits d'offres | export XML organisé par convention de partenariat; ADEP sert à publier depuis un ATS; aucune supposition d'accès au flux partenaire |
| Autres sites carrière | expérimental | découverte Brave puis connecteur public explicite par ATS | dépend de l'ATS; aucune donnée manquante n'est inventée | intégrer uniquement une API/flux officiellement public. Lever est un prochain candidat raisonnable; les sites sans voie propre restent des signaux |
| Brave / web ouvert | socle intégré, signaux seulement | client Brave existant, cache/budget/ledger existants, plan de requêtes borné | URL, titre d'index, extrait, source réelle déduite du domaine et mention éventuelle du 94 | clé Brave et budget existants; aucun second quota; un résultat incomplet devient `RecruitmentSignal`, jamais une fausse offre |

## Décisions d'architecture

`ObservedJobOffer` reste l'observation brute d'une source. La paire
`(source, source_offer_id)` demeure unique. `source` désigne le site qui porte
l'offre (`france_travail`, `linkedin`, `employer_career_site`, etc.), alors que
`discovery_provider` indique le mécanisme qui a trouvé l'URL (`brave_search`, par
exemple). Une offre LinkedIn trouvée par Brave n'est donc jamais étiquetée comme
une offre Brave.

`JobOfferProvider` est le contrat de collecte. Son `provider_id` identifie le
mécanisme exécuté et peut différer de la source finale, notamment pour le web
ouvert. Les pages sont ordonnées et explicitement finales. Une désactivation ne
peut avoir lieu qu'après le succès complet d'un provider qui l'autorise. Pour
Greenhouse, le scope est le board employeur; l'échec ou le rafraîchissement d'un
board ne modifie jamais les offres d'un autre board.

La déduplication cross-source est conservative et calculée sans détruire les
observations. Elle exige la même identité d'entreprise normalisée, un intitulé
normalisé identique, une localisation commune, des contrats compatibles et des
dates proches. Deux observations de la même source ne sont pas fusionnées. Une
publication éloignée de plus de 21 jours reste un épisode distinct. L'agrégat
commercial compte le besoin canonique une fois, mais expose toutes les sources,
URLs et identifiants comme preuves.

Les résultats web incomplets sont persistés dans `RecruitmentSignal`. Le provider
Brave ne visite pas LinkedIn, Indeed, HelloWork ou Leboncoin : il conserve
uniquement ce que le moteur retourne. Une promotion future vers
`ObservedJobOffer` devra fournir explicitement entreprise, intitulé,
localisation 94, URL preuve et identifiant/fingerprint stable.

## Stratégie de requêtes web

Le plan est déterministe, testable et plafonné. Il combine un petit ensemble de
communes, d'intentions de recrutement et de domaines ciblés, puis supprime les
URLs répétées dans le run. Le client utilisé est le client Brave existant : chaque
appel passe donc par la réservation du ledger, le budget mensuel, le hard cap de
run et la limitation de débit déjà en place. Les tests injectent un faux client
et n'effectuent aucun appel réseau.

## Références officielles consultées

- France Travail, API Offres d'emploi : https://www.data.gouv.fr/dataservices/api-offres-demploi
- Greenhouse, Job Board API : https://docs.greenhouse.io/job-board.html
- Lever, Postings API (prochaine étape possible) : https://github.com/lever/postings-api
- HelloWork, partenariats ATS : https://recruteur.hellowork.com/fr/page/nos-partenaires-ats.html
- HelloWork, règles robots : https://www.hellowork.com/robots.txt
- Welcome to the Jungle Solutions, scopes : https://developers.welcomekit.co/scopes
- LinkedIn, règles robots : https://www.linkedin.com/robots.txt
- LinkedIn, Job Posting API : https://learn.microsoft.com/en-us/linkedin/talent/job-postings/api/sync-job-postings
- Indeed, Job Sync API : https://docs.indeed.com/job-sync-api/
- Leboncoin, règles robots : https://www.leboncoin.fr/robots.txt
- Apec, partenariat et export XML : https://corporate.apec.fr/devenir-partenaire

## Prochaines étapes

1. Configurer explicitement un premier board Greenhouse d'employeur pertinent au
   94 et exposer son refresh indépendant dans l'API/UI.
2. Ajouter Lever sur le même scope `employer_career_site`, après vérification de
   sa politique et avec fixtures JSON.
3. Ajouter un vérificateur de pages carrière respectueux de `robots.txt` pour
   promouvoir certains `RecruitmentSignal` en offres structurées.
4. N'activer HelloWork, WTTJ, Apec ou un autre catalogue direct qu'après accès
   public officiellement documenté ou convention partenaire.
