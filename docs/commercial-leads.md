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
