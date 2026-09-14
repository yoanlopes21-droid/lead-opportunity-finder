# Architecture V1

L'application fonctionne entièrement sur le Mac de l'utilisateur : une interface React communique avec une API FastAPI, qui persiste les données dans SQLite.

Les futurs connecteurs (France Travail, SIRENE/INSEE, sites d'entreprises) seront des modules indépendants. Ils produiront des observations et des preuves sourcées ; ils ne modifieront pas directement le scoring. Le scoring créera ensuite une évaluation explicable, distincte des faits collectés.

Nicoka, les envois de messages et les automatisations de contact sont hors périmètre V1.

