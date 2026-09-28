# Historique commercial local

`commercial_relationships` reste la source du statut courant à l’échelle de l’entreprise. `commercial_interactions` conserve uniquement les événements confirmés explicitement par l’utilisateur et leur portée métier (`need_id = source:offer_id`) avec les snapshots historiques utiles. Une copie de texte, l’ouverture du workspace et la préparation d’un brouillon ne créent aucun événement.

Au démarrage, la migration ajoute `commercial_relationships.next_action` si absent et crée `commercial_interactions` avec un `request_id` unique pour éviter les doublons. Elle ne réécrit aucune ligne existante. La date de prochaine action réutilise `commercial_relationships.next_action_at`.

`POST /api/v1/commercial-interactions` valide une entreprise locale connue ou un dossier commercial existant, le canal, le résultat et la date réelle de l’action. La readiness interdit la génération d’une nouvelle sollicitation, mais ne bloque pas la consignation d’un échange réellement survenu. Le statut, l’éventuelle exclusion forte et l’événement sont enregistrés dans une seule transaction. `GET /api/v1/commercial-interactions/{company_key}` charge au plus 30 événements récents à la demande et retrouve aussi l’identité par SIREN confirmé.

Les dates saisies par l’utilisateur sont interprétées dans la timezone métier `Europe/Paris`, converties en UTC avant l’API, stockées en UTC dans SQLite et renvoyées avec une timezone UTC explicite. Les badges « aujourd’hui » et « en retard » sont calculés sur la date Europe/Paris. Une saisie antidatée reste dans le journal mais ne remplace pas l’état courant si un événement plus récent existe.

`next_action_mode` distingue `preserve`, `replace` et `clear`. Un champ vide n’efface donc jamais implicitement une suite existante.

Le dossier `GET /api/v1/commercial-relationships/{id}/dossier` expose les besoins actifs et historiques. Un besoin inactif reste utilisable pour consigner un échange, sans produire de nouveau script ou email. Les confirmations humaines d’existence du besoin ont la portée stricte `need_exists`; elles peuvent lever l’ancienneté de l’offre, jamais l’identité employeur ou l’intermédiation.

Le mapping déterministe se trouve dans `OUTCOME_STATUSES` du service backend. `no_answer`, `switchboard` et `conversation` donnent `contacted` ; `email_requested` et `email_sent` donnent `awaiting_reply` ; `callback_requested` donne `follow_up` ; `position_filled` donne `no_current_need`. Les autres résultats rejoignent leur statut commercial homonyme. `client` et `do_not_contact` utilisent les exclusions fortes existantes. Une date reste facultative pour `follow_up` et obligatoire pour `meeting_scheduled`.

Le dernier événement `email_requested` ouvre le brouillon `email_requested_after_call`. La transition personnalisée reprend uniquement `priority_expressed` saisi dans ce dernier événement. Aucun email ni rappel n’est envoyé automatiquement.
