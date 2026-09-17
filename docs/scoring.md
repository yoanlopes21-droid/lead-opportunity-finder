# Scoring commercial V1

Le moteur est déterministe, ne fait aucun appel externe et ne persiste pas les
scores. Il recalcule une évaluation explicable à partir des opportunités actives
et de l'enrichissement local éventuellement disponible.

## Barème

- besoin direct : 35 points maximum ;
- signaux latents : 25 points maximum ;
- pertinence commerciale : 20 points maximum ;
- accessibilité commerciale : 10 points maximum ;
- fraîcheur et qualité des preuves : 10 points maximum.

Les paliers exacts sont les suivants :

- besoin direct : 6 / 13 / 19 / 24 points pour 1 / 2–4 / 5–9 / 10+ offres,
  puis 1 point par intitulé au-delà du premier (maximum 6), 3 points pour
  plusieurs besoins simultanés et 2 points lorsqu'une offre a 21 jours ou
  moins ;
- signaux latents : 6 / 6 / 5 points lorsqu'au moins une offre dépasse 21 /
  45 / 90 jours, 5 points pour une observation récurrente et 3 points pour
  plusieurs lieux ;
- pertinence : privé 20, inconnu 12, nonprofit 10, public 5 et intermédiaire
  identifié 3 ;
- accessibilité : identité high confidence 7, review 5, ambiguous 3,
  not found 2, generic/error 1, puis jusqu'à 3 points pour une identité
  confirmée, une localisation et plusieurs sources ;
- fraîcheur : 6 / 4 / 2 / 0 points pour une offre la plus récente à 7 / 21 /
  45 jours ou au-delà, puis 2 points multi-source, 1 point d'observation
  récurrente et 1 point lorsque les dates sont disponibles.

Les entités publiques reçoivent une pénalité explicite de 20 points et les
intermédiaires identifiés une pénalité de 15 points. Elles restent visibles :
aucune catégorie ou statut d'enrichissement ne provoque une exclusion. Pour
éviter que le volume d'un intermédiaire domine le classement, son sous-score de
besoin direct est plafonné à 20 points.

Un ajustement commercial distinct des cinq sous-scores est appliqué seulement
lorsqu'une tranche d'effectif DINUM est réellement connue : +3 pour `1-2` à
`20-49`, +1 pour `50-99` à `200-249`, -8 pour `250+`, et 0 pour `0`,
`unknown`, une valeur absente ou non reconnue. Aucune taille n'est inférée.

Le traitement public s'applique aussi à des marqueurs de nom stricts tels que
`Ville de`, `Mairie`, `Commune de`, `Conseil départemental/régional`,
`Département de`, `Région`, `CCAS` et `Centre communal d'action sociale`.
Le traitement intermédiaire s'applique au statut existant, au code NAF confirmé
de division 78, ainsi qu'aux marqueurs `recrutement`, `intérim` ou `staffing`
et à une courte liste d'alias explicites. Ces signaux sont tous exposés dans
les ajustements du résultat.

Les seuils, regroupés dans `ScoringPolicy`, sont :

- 75–100 : 🔥 priorité très forte ;
- 55–74 : 🟢 bon prospect ;
- 35–54 : 🟠 à surveiller / priorité moyenne ;
- 0–34 : ⚪ faible priorité.

Chaque résultat expose les cinq sous-scores, les raisons positives, les
pénalités et les signaux descriptifs utilisés. L'absence de confirmation DINUM
limite seulement l'accessibilité ; elle ne réduit pas artificiellement le besoin
commercial. Les clients actuels et prospects récents restent un futur traitement
manuel via la liste d'exclusion.
