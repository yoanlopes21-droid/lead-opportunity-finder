# Batches durables de contactabilité

Les batches de contactabilité sont optionnels et ne lancent aucun appel sans provider
configuré. Ils sont distincts des enrichissements DINUM et n’altèrent ni les offres,
ni le scoring, ni les identités juridiques existantes.

## Persistance et reprise

Un run persiste intégralement sa sélection de cibles avant le premier appel. Chaque item
conserve seulement un snapshot minimal de la cible, son fingerprint et des erreurs
nettoyées : jamais de token ou de payload provider brut. Les résultats sont enregistrés
cible par cible via les services `ContactPoint`, `PersonContact` et `ContactEvidence`.

Un resume conserve l’ordre original. Les items `completed`, `cached` et `not_applicable`
ne sont pas rejoués ; un item `processing` orphelin redevient `pending`. Les erreurs
terminales restent des erreurs. Une sélection manquante ou incohérente est refusée.

## Cache Societe.com

Le cache est indépendant par ressource et par fingerprint de cible :

- `contact` : 30 jours ;
- `directors` : 90 jours ;
- `not_found` : 30 jours, pour les deux ressources.

Un changement de SIREN, de portée, de clé commerciale, de statut d’identité ou de nom
d’identité invalide le cache. Une cible peut donc nécessiter zéro, un ou deux appels.
Un succès `contact` reste utilisable si `directors` échoue.

Les erreurs transitoires (`timeout`, limitation de débit, erreur serveur ou réseau) sont
retentées une seule fois par ressource avec backoff configurable. Les erreurs permanentes
ne sont pas retentées aveuglément. Après trois cibles consécutives en erreur transitoire,
le run est arrêté en `failed` afin de limiter les coûts.

## CLI locale

Avec un token configuré dans l’environnement local, les commandes sont :

```bash
cd backend
../.venv/bin/python -m app.cli.contact_enrichment new --provider societe_com --department 94 --limit 50
../.venv/bin/python -m app.cli.contact_enrichment resume --run-id RUN_ID
```

Sans token, le CLI s’arrête proprement avant de créer un run ou de consommer des crédits.
La sortie ne contient que des compteurs agrégés. Tout premier run réel reste une action
manuelle à valider par l’utilisateur.
