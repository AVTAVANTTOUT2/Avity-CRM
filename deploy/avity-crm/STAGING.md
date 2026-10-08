# Staging synthétique

Le VPS reste en lecture seule. L'environnement de qualification est la VM locale
Colima `crm-foundation` : 6 CPU, 6 Gio de RAM, disque 30 Gio, Docker 29/Compose 5.
Son contexte est `colima-crm-foundation` ; le contexte Docker par défaut ne change
pas. L'application AMD64 utilise Rosetta sur le Mac ARM64 ; DB/Redis/Nginx restent
natifs. Ces essais ne mesurent pas les performances du VPS.

Projet : `avity-crm-staging`. État privé : `/var/lib/avity-crm-staging` dans la VM.
URL : **http://localhost:3021**. Compte et données sont fictifs, secrets propres,
sans snapshot de production. Aucun domaine ou tunnel Cloudflare n'est créé.

## Construire et démarrer

Dans un checkout propre du SHA choisi :

```bash
colima start crm-foundation --activate=false --cpu 6 --memory 6 --disk 30 \
  --vm-type vz --vz-rosetta --ssh-agent=false --mount "$PWD:w"
DOCKER_CONTEXT=colima-crm-foundation bash deploy/avity-crm/build-image.sh
candidate_sha=$(git rev-parse HEAD)
colima ssh --profile crm-foundation -- sudo "$PWD/deploy/avity-crm/staging.sh" init "$candidate_sha"
colima ssh --profile crm-foundation -- sudo "$PWD/deploy/avity-crm/staging.sh" up
colima ssh --profile crm-foundation -- sudo "$PWD/deploy/avity-crm/staging.sh" status
```

Précharger dans ce seul contexte les dépendances épinglées de `compose.yml` et
`staging/compose.yml`. `up` refuse de télécharger/construire une image et contrôle
son label SHA. Une image Actions au même SHA peut aussi être chargée après
vérification de son `SHA256SUMS` avec `docker --context colima-crm-foundation load`.
Pour la sauvegarde, produire `git archive --format=tar.gz SHA`, puis installer
la copie privée `/var/lib/avity-crm-staging/artifacts/source.tar.gz` via un
répertoire partagé dédié. Ne pas monter de secrets/snapshots de production ni la
clé privée de sauvegarde. L'export qualifié monte uniquement son sous-dossier
synthétique du stockage Mac existant.

`init` vérifie le port libre et refuse d'écraser les identifiants existants.
`AVITY_CRM_STAGING_PORT` peut choisir un autre port, sauf 3020 ; `SERVER_URL` doit
correspondre à son origine HTTP loopback. `AVITY_CRM_STAGING_ROOT` peut choisir
un état privé distinct ; les chemins de production sont refusés. Pour une nouvelle
image, préserver les secrets et modifier seulement `GIT_SHA` après son chargement.

## Isolation et commandes

L'override réutilise le Compose de base. App/worker/DB/Redis rejoignent uniquement
le réseau interne propre au projet, sans accès sortant. Un Nginx sans privilèges
publie seulement 127.0.0.1:3021. DB/Redis n'ont aucun port publié. Les volumes sont
`avity-crm-staging_{db-data,redis-data,server-local-data}`. Les intégrations mail,
calendrier, télémétrie et analytics restent désactivées. Le worker reçoit 1,5 Gio
uniquement en staging : la génération du SDK sous émulation dépassait 1 Gio.
Les limites de production restent inchangées.

Dans la VM dédiée, root Linux ; les scripts ne montrent jamais les identifiants :

```bash
python3 deploy/avity-crm/tests/qualify_staging.py bootstrap
python3 deploy/avity-crm/tests/qualify_staging.py create
python3 deploy/avity-crm/tests/qualify_staging.py check
deploy/avity-crm/staging.sh backup
deploy/avity-crm/staging.sh stop
deploy/avity-crm/staging.sh up
python3 deploy/avity-crm/tests/test_backup.py
python3 deploy/avity-crm/tests/test_export.py
python3 deploy/avity-crm/tests/test_staging.py
python3 deploy/avity-crm/tests/test_restore.py
```

Bootstrap/create sont des actions initiales uniques. Terminer l'onboarding dans
l'interface et ignorer les invitations, sans envoi. Check valide login, fiche,
refus anonyme/inscription, CSRF cookies, santé et upgrades. Vérifier aussi logout,
rafraîchissement, persistance, clair/sombre/mobile et absence d'erreur navigateur.
La construction Actions exécute aussi typage/lint/formatage/tests frontend.

Pour réinitialiser les seuls volumes de ce staging :

```bash
deploy/avity-crm/staging.sh reset --confirm-staging-data-loss
```

Aucun prune global. Les wrappers refusent les projets étrangers, secrets de
production et paramètres détournant le projet ou les fichiers Compose.

La restauration utilise `avity-crm-staging-restore`, état privé
`/var/lib/avity-crm-staging-restore`, URL http://localhost:3022. Voir
[BACKUP.md](BACKUP.md). Son arrêt utilise le Compose archivé avec les variables
`AVITY_CRM_PROJECT=avity-crm-staging-restore` et
`AVITY_CRM_ENV_FILE=/var/lib/avity-crm-staging-restore/avity-crm.env`, puis `stop`.
