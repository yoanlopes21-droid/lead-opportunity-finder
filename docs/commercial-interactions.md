# Historique commercial local

`commercial_relationships` reste la source du statut courant. `commercial_interactions` conserve uniquement les événements confirmés explicitement par l’utilisateur. Une copie de texte, l’ouverture du workspace et la préparation d’un brouillon ne créent aucun événement.

Au démarrage, la migration ajoute `commercial_relationships.next_action` si absent et crée `commercial_interactions` avec un `request_id` unique pour éviter les doublons. Elle ne réécrit aucune ligne existante. La date de prochaine action réutilise `commercial_relationships.next_action_at`.

`POST /api/v1/commercial-interactions` valide une entreprise locale connue, son état de prospection, le canal, le résultat et la date réelle de l’action. Le statut, l’éventuelle exclusion forte et l’événement sont enregistrés dans une seule transaction. `GET /api/v1/commercial-interactions/{company_key}` charge au plus 30 événements récents à la demande.

Le mapping déterministe se trouve dans `OUTCOME_STATUSES` du service backend. `no_answer`, `switchboard` et `conversation` donnent `contacted` ; `email_requested` et `email_sent` donnent `awaiting_reply` ; `callback_requested` donne `follow_up` ; `position_filled` donne `no_current_need`. Les autres résultats rejoignent leur statut commercial homonyme. `client` et `do_not_contact` utilisent les exclusions fortes existantes. Une date reste facultative pour `follow_up` et obligatoire pour `meeting_scheduled`.

Le dernier événement `email_requested` ouvre le brouillon `email_requested_after_call`. La transition personnalisée reprend uniquement `priority_expressed` saisi dans ce dernier événement. Aucun email ni rappel n’est envoyé automatiquement.
