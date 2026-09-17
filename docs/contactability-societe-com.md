# Provider de contactabilité Societe.com API Pro

Le provider `societe_com` est optionnel. Il utilise uniquement l’API Pro structurée et ne
scrape pas les pages publiques de Societe.com. Sans configuration, il renvoie
`not_configured` et le reste de l’application continue de fonctionner.

La configuration attendue est `LEAD_FINDER_SOCIETE_COM_API_TOKEN`. Un abonnement et un
token API valides sont nécessaires, et les appels peuvent consommer des crédits. Le token
ne doit jamais être commité, journalisé, placé dans une URL ou stocké dans une preuve.

## Portée V1

Le provider appelle seulement :

- `GET /entreprise/{numid}/contact` ;
- `GET /entreprise/{numid}/dirigeants`.

Il est applicable uniquement à une cible `company` qui possède un SIREN confirmé par un
matching juridique `matched_high_confidence`. Il ne recherche jamais une entreprise par
nom. Les cibles locales, intermédiaires, ambiguës ou sans SIREN sont `not_applicable`.

Les téléphones, emails et sites sont des faits de portée `company`; ils ne sont jamais
copiés vers les sous-opportunités locales. Une cohérence SIREN/SIRET et raison sociale
permet au maximum `high_confidence`, avec `source_verified`. Une incohérence d’identifiant
rejette le résultat; une incohérence de nom impose une revue.

Les dirigeants sont limités aux personnes physiques et à leur identité/fonction
professionnelle publique. Les adresses personnelles, bénéficiaires effectifs et données
privées sans utilité B2B ne sont pas conservés. Un dirigeant disponible n’est pas désigné
automatiquement comme contact recommandé.

Le fingerprint de tentative dépend du provider, de la cible, de sa portée, du SIREN et du
statut de matching. Les TTL préparés sont de 30 jours pour les coordonnées structurées et
90 jours pour les dirigeants. Aucun batch ou appel automatique n’est activé à ce stade.
