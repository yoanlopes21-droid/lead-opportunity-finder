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

Le SIREN confirmé est la clé de rapprochement prioritaire. Une exclusion sans
SIREN continue de s'appliquer à la `company_key` exacte après enrichissement
juridique. Une exclusion rattachée à un autre SIREN confirmé ne s'applique pas
sur le seul nom. Aucun rapprochement approximatif n'est appliqué.

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

## Entrée commerciale fiable

`GET /api/v1/commercial-leads/{company_key}/approach-context?department=94`
compose en lecture seule le contexte d'une entreprise possédant au moins une
offre active dans le département. La clé est celle exposée par la liste des leads.
Une clé absente renvoie 404. Aucun provider, enrichissement réseau ou envoi n'est
déclenché. La route lit seulement les noms des offres actives du département,
puis charge les enregistrements complets de l'entreprise sélectionnée.

Le contexte réutilise la canonicalisation, le score, l'éligibilité et la stratégie
de contact existants. Il distingue le nom observé de l'identité légale confirmée,
et utilise exclusivement la localisation de l'offre d'entrée pour le besoin.
`canonical_need_count` compte les besoins canoniques; `source_listing_count`
compte leurs annonces sources. Aucun de ces compteurs ne prétend décrire le
nombre de postes à pourvoir.

`readiness` prend l'une des valeurs suivantes :

- `ready_to_contact` : canal professionnel direct ou recrutement exploitable ;
- `routing_required` : standard, email général ou page de contact à router ;
- `channel_missing` : approche préparable, canal non trouvé ;
- `verify_contact` : une coordonnée existe, mais sa portée ou sa fiabilité doit
  être vérifiée ;
- `verify_offer` : dernière observation du besoin vieille de plus de sept jours ;
- `verify_employer` : l'attribution de l'annonce doit être vérifiée avant un
  contenu client définitif ;
- `intermediary_not_employer` : ne pas viser un employeur final supposé ;
- `suspended` : exclusion active ou statut « ne plus contacter » / client.

`approach_preparable` reste vrai sans SIREN, personne ou canal. Il reste aussi
vrai pour `verify_employer`, ce qui autorise une préparation interne, mais
`contact_now_possible` est alors faux. Les contacts candidats comprennent ceux
qui sont écartés, avec `use`, `reason_codes`, portée et URLs de provenance.
`usable_facts` et `prohibited_claims` serviront au futur compositeur; ce bloc ne
produit ni texte commercial ni engagement contractuel. L'historique des lignes
de suivi inactives demeure visible et bloque une affirmation automatique de
« premier contact ».
