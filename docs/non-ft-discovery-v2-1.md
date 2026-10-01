# V2.1 — découverte commerciale hors France Travail

## Architecture

Cette branche réutilise `JobOfferProvider`, `ObservedJobOffer`, la consolidation en besoins et entreprises, les exclusions, le score commercial, les signaux, la prospection et l'export. Un `discovery_provider` décrit comment une page a été trouvée ; `source` décrit l'observation du besoin. Une recherche Brave ne devient donc jamais une offre par sa seule présence dans les résultats.

`non_ft_only` est calculé sur les observations actives du besoin canonique : au moins une observation confirmée non FT et aucune observation FT connue pour ce même besoin. L'arrivée ultérieure de FT change ce résultat sans effacer la première provenance. Le score commercial reste indépendant. En cas de preuves multiples, la page de l'employeur puis l'ATS sont présentés en premier ; les observations FT restent conservées. La déduplication existante, prudente sur les rôles distincts, est conservée.

## Entreprises du 94 et domaines

L'[API Recherche d'entreprises de la DINUM](https://www.data.gouv.fr/dataservices/api-recherche-dentreprises) fournit une page de PME actives avec établissement dans le 94 par run, au maximum 25 lignes. Un seul établissement par SIREN est retenu. Le curseur est durable. Un seed est une entreprise à inspecter, jamais une preuve de recrutement. Exclusions et clients actifs sont vérifiés avant une recherche web. Les domaines officiels déjà vérifiés sont prioritaires. Une URL issue de Brave reste candidate jusqu'aux contrôles d'identité et de propriété du module `official_web` ; les résultats ambigus restent à revoir.

Le run manuel examine dans l'ordre les boards enregistrés, les sites employeurs vérifiés, les seeds locaux et enfin des recherches Brave ciblées par entreprise et commune. Il vise 20 à 30 leads et borne chaque étape. Il enregistre son état, ses compteurs, les runs enfants et la demande d'arrêt ; un run interrompu au redémarrage est marqué échoué et les seeds peuvent être repris au run suivant. Le cache Brave et `brave_usage_events` restent la source du décompte ; le cap par run et la réserve mensuelle sont appliqués avant chaque appel. Les seeds sont paginés côté API.

## Offres directes

Le collecteur de site officiel exploite uniquement un domaine HTTPS vérifié. Il suit les liens carrière de la page d'accueil, puis un sitemap public si nécessaire, avec un petit nombre de pages. Les requêtes réutilisent le fetcher SSRF-safe, la politique robots, une limite de taille et un débit par domaine. Il extrait le JSON-LD [`JobPosting`](https://developers.google.com/search/docs/appearance/structured-data/job-posting) lorsque le rôle, la description, une `datePosted` récente, l'attribution et une localisation explicite du poste dans le 94 sont présents. `validThrough` expiré exclut l'offre. La commune d'une entreprise ne prouve jamais celle du poste. L'absence de JSON-LD ne produit rien. Un crawl borné ne peut pas prouver qu'une offre a disparu : il ne désactive donc pas les offres non revues. Une expiration explicite retire l'offre des opportunités actives.

Les liens ATS sont reconnus sur le site officiel. Le board n'est enregistré automatiquement que si un lien unique et son identifiant correspondent sans ambiguïté au nom de l'entreprise ; sinon le candidat est conservé pour revue. Les signaux web tiers, annuaires et agrégateurs ne sont pas promus. Une promotion exige une page d'offre officielle revérifiée avec `JobPosting` récent et géographie du poste explicite.

## ATS publics étudiés

| Fournisseur | Décision | Base publique |
| --- | --- | --- |
| Greenhouse, Lever | Conservés | Providers V1 existants. |
| Ashby | Ajouté | [API publique des offres](https://developers.ashbyhq.com/docs/public-job-posting-api), board public sans clé, résultats localisés. L'API interne `jobPosting.list` exige une authentification et n'est pas utilisée. |
| Workable | Ajouté | [Jobs publics du compte](https://workable.readme.io/reference/jobs-1), accès public aux offres du sous-domaine. L'API SPI privée n'est pas utilisée. |
| SmartRecruiters | Non retenu | [Documentation des API](https://developers.smartrecruiters.com/docs/posting-api) : l'accès public stable sans authentification n'a pas été établi avec assez de certitude pour cette version. |
| Recruitee, Teamtailor | Non retenus | Aucun contrat public suffisamment vérifié dans cette étude ; pas de scraping d'interface. |

Un board ATS actif a son propre refresh complet ; seule sa portée peut désactiver ses observations disparues après succès. Un échec de requête ne ferme aucune offre.

## Produit et limites

Nouveautés propose les filtres Toutes, Hors France Travail, France Travail, Employeur direct et ATS, un compteur hors FT et des badges de provenance sur la LeadCard. Les mêmes exclusions, cartes, actions de contact et suivi sont utilisés. L'export Excel ajoute la provenance principale, le statut hors FT et les sources observées. Sources propose le run manuel et les deux nouveaux types de boards.

La couverture dépend de domaines officiels confirmés, de pages publiques structurées et de boards réellement associés à l'employeur. Les sites qui ne publient qu'en HTML non structuré, les offres sans localisation du poste et les liens ATS ambigus restent des signaux. Le pilote local et ses données commerciales restent dans un rapport ignoré par Git ; ni base ni secret ne sont versionnés. La base et la configuration V1 ne sont jamais migrées par cette branche.
