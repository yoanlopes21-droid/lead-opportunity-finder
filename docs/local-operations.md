# Exploitation locale V1

Lead Opportunity Finder dispose de deux modes distincts :

- en usage quotidien, FastAPI sert l’API et le build React sur `http://127.0.0.1:8000` dans un seul processus ;
- en développement, FastAPI et Vite restent séparés, avec le proxy Vite documenté dans `docs/development.md`.

Ce choix évite deux processus et deux terminaux au quotidien, sans modifier l’architecture de développement. Le launcher pointe vers le projet courant : il n’embarque ni copie du code, ni base SQLite.

## Prérequis du mode quotidien

Le projet doit contenir :

- `.venv` avec les dépendances backend et `uvicorn` ;
- `.env` local ;
- `frontend/dist/index.html`, créé par `npm run build` ;
- la base SQLite configurée.

Le double-clic n’installe jamais de paquet et ne déclenche aucun accès réseau d’installation. Node n’est pas requis pour un lancement quotidien si le build existe ; il reste requis pour reconstruire le frontend.

## Construire le launcher macOS

Depuis la racine du projet :

```bash
./scripts/build_macos_launcher.sh
```

Le builder détecte la racine réelle et crée `~/Applications/Lead Opportunity Finder.app`. Le chemin absolu local n’est injecté que dans le bundle généré, jamais dans une source suivie par Git. Un launcher existant est remplacé seulement après compilation et signature réussies ; il est restauré automatiquement si le remplacement échoue.

Le launcher utilise AppleScript et les mécanismes macOS standards, sans dépendre d’un numéro de version macOS. Après une mise à jour majeure, le diagnostic indique si `.venv`, le build ou un autre prérequis manque ; le launcher peut alors être reconstruit avec la même commande.

Au premier double-clic, le launcher vérifie l’environnement, démarre l’application en arrière-plan, attend le health check, puis ouvre le navigateur. Aucun Terminal persistant n’apparaît. Au double-clic suivant, il propose **Ouvrir**, **Arrêter** ou **Annuler**.

Si le projet se trouve dans un dossier protégé par macOS (`Bureau`, `Documents` ou `Téléchargements`), macOS peut demander une autorisation locale au premier lancement. Elle est nécessaire uniquement pour lire le code et écrire les données du projet sélectionné. Autoriser seulement ce dossier ; ne pas accorder l’Accès complet au disque. En cas de déplacement du projet, reconstruire le launcher afin qu’il pointe vers le nouvel emplacement.

## Gestionnaire local

Les commandes de diagnostic restent disponibles, sans être nécessaires à l’usage quotidien :

```bash
.venv/bin/python scripts/lead_finder_manager.py start
.venv/bin/python scripts/lead_finder_manager.py stop
.venv/bin/python scripts/lead_finder_manager.py status
.venv/bin/python scripts/lead_finder_manager.py open
.venv/bin/python scripts/lead_finder_manager.py db-info
```

Le gestionnaire verrouille les opérations concurrentes et conserve l’identité du processus dans `data/runtime/lead_finder.pid.json`. Avant tout signal, il vérifie le PID, son heure de démarrage et la commande `uvicorn` exacte. Il ne tue jamais un processus uniquement parce qu’il utilise le port 8000.

Si le port 8000 répond avec le marqueur health de Lead Opportunity Finder, l’instance est réutilisée. Si une autre application occupe le port, le démarrage échoue avec un message clair et aucun processus n’est interrompu.

Les fichiers `data/runtime/backend.log` et `data/runtime/manager.log` sont locaux et ignorés par Git. Le log backend est réinitialisé à chaque démarrage ; le log manager effectue une rotation minimale à 1 Mo.

## Base SQLite canonique

Par défaut, la base est `data/lead_opportunity_finder.sqlite3` sous la racine du projet. Toute URL SQLite relative fournie par `LEAD_FINDER_DATABASE_URL` est résolue depuis la racine du projet, jamais depuis le dossier courant.

`db-info` et `status` indiquent le chemin absolu réellement utilisé, l’existence, la taille, l’intégrité, la version SQLite déclarée et les comptes des tables principales. Ces informations restent locales et ne sont pas exposées dans l’interface normale.

## Recherches interrompues

Au démarrage, une `SearchRun` laissée dans l’état `queued`, `running` ou `stopping` est convertie une seule fois en `stopped`, avec la raison `interrupted_restart` et le message « Interrompue lors du redémarrage de l’application ». Les résultats partiels terminés sont conservés ; un item en cours redevient `pending`.

Aucun fournisseur ni appel réseau n’est relancé pendant cette récupération. L’utilisateur peut ensuite cliquer volontairement sur **Reprendre**. La récupération est idempotente.

## Sauvegarde

Créer un snapshot cohérent de SQLite :

```bash
.venv/bin/python scripts/lead_finder_manager.py backup
```

La commande utilise l’API Backup de SQLite, compatible avec WAL, vérifie `PRAGMA integrity_check`, crée un nom UTC unique dans `data/backups/` et n’écrase jamais une sauvegarde existante.

La configuration privée est distincte. Pour demander explicitement une copie locale sensible de `.env` :

```bash
.venv/bin/python scripts/lead_finder_manager.py backup --include-env
```

La copie est marquée `env-sensitive`, protégée en mode `0600`, ignorée par Git et ne doit jamais être envoyée à un service externe.

## Restauration

Restaurer un snapshot SQLite :

```bash
.venv/bin/python scripts/lead_finder_manager.py restore data/backups/lead_opportunity_finder.backup-DATE.sqlite3
```

La commande :

1. vérifie le fichier et son intégrité ;
2. arrête l’instance si elle appartient au gestionnaire ;
3. refuse de tuer une instance non identifiée ou un processus étranger ;
4. crée un snapshot `pre-restore` de la base courante ;
5. restaure via l’API SQLite dans un fichier temporaire validé puis remplacé atomiquement ;
6. supprime seulement les sidecars WAL/SHM de la base explicitement ciblée, application arrêtée ;
7. redémarre FastAPI, qui applique les migrations additives ;
8. attend le health check et produit un nouveau diagnostic.

En cas de doute, conserver la sauvegarde `pre-restore`. Ne jamais remplacer manuellement la base pendant que l’application écrit.

## Changer de Mac

Sur l’ancien Mac :

1. arrêter l’application ;
2. vérifier que le checkpoint Git utile est poussé ;
3. exécuter `backup --include-env` ;
4. transférer de manière privée le snapshot SQLite et la copie sensible de `.env` ;
5. noter les versions/prérequis Python et Node utilisés.

Sur le nouveau Mac :

1. cloner le dépôt au checkpoint voulu ;
2. recréer `.venv` et installer les dépendances backend selon `docs/development.md` ;
3. exécuter `npm ci` puis `npm run build` dans `frontend/` ;
4. replacer `.env` à la racine avec des permissions privées ;
5. placer une base initiale valide au chemin canonique, puis utiliser `restore` pour le snapshot transféré ;
6. vérifier `db-info`, le health check et les comptes principaux ;
7. reconstruire le launcher ;
8. ouvrir l’application et vérifier les écrans principaux.

## Mettre à jour l’application

Le code Git, SQLite, `.env` et le launcher restent découplés. Pour une mise à jour future :

1. arrêter l’application ;
2. créer une sauvegarde ;
3. mettre à jour le dépôt en fast-forward ;
4. mettre à jour les dépendances seulement si les manifestes ont changé ;
5. reconstruire le frontend si nécessaire ;
6. démarrer : les migrations additives sont exécutées automatiquement ;
7. reconstruire le launcher seulement si son chemin ou ses sources ont changé ;
8. vérifier health, `db-info` et les comptes importants.

Une base issue de la version précédente reste au chemin canonique et reçoit les migrations additives au démarrage. La base n’est jamais copiée dans le bundle `.app`.
