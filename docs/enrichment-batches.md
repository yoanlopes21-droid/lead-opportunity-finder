# Enrichissements d'entreprises par lots

## Évolution sûre du schéma

Les tables historiques `company_enrichments` et `enrichment_runs` ne sont ni
recréées ni modifiées. Deux tables complémentaires sont ajoutées avec
`checkfirst=True` :

- `company_enrichment_details` contient l'explication du dernier résultat ;
- `enrichment_run_items` contient la sélection durable et l'état de chaque
  élément d'un run.

Cette stratégie est volontairement additive pour SQLite. Les enrichissements
antérieurs restent valides, même s'ils ne possèdent pas de ligne de détail.

## Cycle de vie et reprise

La sélection complète est enregistrée en `pending` avant le premier appel au
fournisseur. Un élément passe ensuite à `processing`, puis à `completed`,
`cached` ou `error`. Les éléments `completed` et `cached` ne sont jamais
retraités lors de la reprise du même run.

Un arrêt brutal peut laisser un élément en `processing`. Lors d'une reprise,
il est remis à `pending` et rejoué. Cette répétition est sûre : le résultat est
stocké par upsert avec la clé `(company_key, provider)`. Son compteur de
tentatives conserve la tentative commencée avant l'interruption.

Les erreurs définitives déjà enregistrées restent `error` dans le même run.
Les éléments encore `pending` sont repris dans leur ordre de sélection. Une
série d'erreurs transitoires atteignant le seuil de sécurité laisse le run en
`failed` et les éléments restants en `pending`.

Un ancien run créé avant l'introduction de `enrichment_run_items` ne peut pas
être repris automatiquement : sa sélection exacte n'est pas reconstructible.
La commande refuse donc sa reprise sans modifier son statut.

## Cache

Un résultat `matched_high_confidence` est marqué `cached` sans appel distant
si son fingerprint est identique et s'il reste dans le TTL configuré (30 jours
par défaut). Un changement du nom, de la clé, du département, des communes ou
des localisations modifie le fingerprint et impose une nouvelle tentative.
Les résultats ambigus, à revoir ou non trouvés ne sont jamais promus par le
cache.

## Exécution locale longue durée

Depuis la racine du projet :

```bash
cd backend
../.venv/bin/python -m app.cli.enrichment new --limit 120 --department 94
```

Pour reprendre un run existant :

```bash
cd backend
../.venv/bin/python -m app.cli.enrichment resume --run-id RUN_ID
```

La sortie périodique contient uniquement l'identifiant du run et des
compteurs agrégés. Aucun nom d'entreprise, payload fournisseur ou secret n'est
journalisé. Codes de sortie : `0` pour `completed`, `2` pour
`completed_with_errors`, `3` pour `failed` et `4` lorsqu'une reprise ne peut
pas être effectuée de manière sûre.
