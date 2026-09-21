# Commercial leads V1

Les leads commerciaux sont recalculés localement à partir des offres actives,
de l'enrichissement DINUM et du score commercial. Ils ne sont pas persistés.

## Exclusions locales

La table additive `commercial_exclusions` conserve les exclusions sans toucher
aux entreprises ni aux offres. Elle contient une clé entreprise normalisée, un
SIREN optionnel, un instant de début et une expiration optionnelle.

- `current_client` reste actif après son début ;
- `recent_prospect` est actif jusqu'à son expiration exclusive ;
- `manual_exclusion` reste actif sans expiration.

Le SIREN confirmé est la clé de rapprochement prioritaire. Sans SIREN confirmé,
la comparaison utilise uniquement une `company_key` exacte : aucun rapprochement
approximatif n'est appliqué.

## Contrat d'import futur

Un futur lecteur CSV/XLSX peut transmettre au parseur pur une ligne avec :
`company_name`, `siren` optionnel, `exclusion_type`, `expires_at` optionnel et
`reason` optionnelle. Le parseur produit des entrées validées mais ne lit aucun
fichier et ne persiste rien.

## Requête de leads

`list_commercial_leads` filtre les exclusions par défaut, avec une option d'audit
pour les inclure. Les filtres disponibles sont le département, la catégorie, le
secteur, le score minimum et une pagination simple. Le tri est déterministe :
score décroissant, puis nom et clé entreprise.

## Contactabilité exposée

`GET /api/v1/commercial-leads` conserve tous ses champs V1 et ajoute, pour chaque
lead, des champs de lecture seule :

- `contacts` : coordonnées normalisées, leur portée (`company`, `local`,
  `intermediary` ou `unknown`), état de vérification, fraîcheur et preuves
  compactes ;
- `people` : personnes actives pertinentes, leur rôle sourcé et les identifiants
  de leurs coordonnées liées ;
- `contact_strategy` : recommandation recalculée à la demande (cible, canal,
  alternatives, confiance, avertissements, informations manquantes et preuves) ;
- `contactability_summary` : statut compact du site officiel et contexte de
  recrutement utilisable avant prise de contact.
- `active_job_offers` : offres actives compactes de chaque lead de la page,
  triées par publication décroissante puis de manière déterministe. Elles ne
  contiennent ni description complète ni payload source ; elles sont composées
  à partir de la lecture déjà nécessaire à l'agrégation, sans requête par lead.

La réponse conserve les offres actives complètes pour la page demandée afin que
l'interface locale puisse les dévoiler à la demande. L'interface n'en affiche
que quelques-unes par défaut ; l'API reste paginée au niveau des leads.

Les coordonnées locales restent liées à leur `local_key`; elles sont également
référencées dans la sous-opportunité correspondante via `contact_point_ids` et
`person_contact_ids`. Une coordonnée d'entreprise n'est jamais propagée à une
sous-opportunité locale.

Le calcul ne lance aucun provider, aucun appel réseau, ni aucune recherche Brave.
Les données Societe.com éventuellement déjà persistées sont lues comme toute
autre provenance, sans configuration ni appel à ce fournisseur. Les coordonnées
`review_needed`, ambiguës, obsolètes ou rejetées restent explicables dans la
réponse mais ne sont pas promues en canal recommandé fiable.

Les relations sont chargées par ensembles pour la page demandée (coordonnées,
personnes, preuves et statuts web), puis la stratégie est calculée en mémoire;
il n'y a pas de requête contacts/personnes/preuves par lead.
