# Avity-CRM : installation depuis le fork

Fork : https://github.com/AVTAVANTTOUT2/Avity-CRM

Base publiée : [`twenty/v2.45.0`](https://github.com/twentyhq/twenty/releases/tag/twenty/v2.45.0),
SHA upstream `7e1431c84adbb264db98ab8b8d008a8401b6d4d6`.
Branche de déploiement : `deploy/twenty-v2.45.0`.
Les licences et mentions Twenty restent dans les fichiers d'origine.

Le lot prépare un [staging isolé](STAGING.md), une [sauvegarde complète](BACKUP.md)
et l'[identité Avity](BRANDING.md). Sa [promotion et son rollback](DEPLOYMENT.md)
exigent une autorisation distincte ; cette PR ne change pas la production.

## Construction et installation

Le workflow **Avity CRM image** construit la cible officielle `twenty`
(interface et serveur) depuis une archive Git vierge du commit du fork,
sans réutiliser des fichiers de compilation locaux. L'image
`avity-crm:git-<SHA complet>` inclut le SHA dans son label OCI `revision`.
L'artefact Actions contient l'image, sa provenance et `SHA256SUMS`.
Le serveur et le worker utilisent exactement cette même image.

La construction est exécutée hors du VPS pour préserver ses ressources.
Pour reconstruire localement sur un hôte Docker adapté :

```bash
bash deploy/avity-crm/build-image.sh
```

Le projet Compose est `avity-crm`, installé dans
`/opt/avity-crm/releases/<SHA>`, avec `/opt/avity-crm/current` comme lien actif.
Les secrets restent dans `/etc/avity-crm/avity-crm.env` (root, mode 0600).
Ils doivent être générés individuellement, sans affichage, puis sauvegardés :
`ENCRYPTION_KEY` (32 octets en base64), `APP_SECRET` et `PG_DATABASE_PASSWORD`.
Le fichier contient également `GIT_SHA`, `HTTP_PORT=3020` et
`SERVER_URL=https://crm.avity.fr` pour l'installation publique actuelle.

Après vérification du checksum, charger l'artefact avec `docker load` et
installer l'archive Git du même SHA. Démarrer avec :

```bash
sudo /opt/avity-crm/current/deploy/avity-crm/compose.sh up -d --no-build --pull never
sudo /opt/avity-crm/current/deploy/avity-crm/compose.sh ps
```

Les volumes `avity-crm_db-data`, `avity-crm_redis-data` et
`avity-crm_server-local-data` sont propres au CRM. PostgreSQL et Redis n'ont
aucun port publié. Seul `127.0.0.1:3020` publie l'interface.
Ne jamais lancer le Compose upstream pour cette installation.

## Accès et administration

L'URL publique est **https://crm.avity.fr**. Cloudflare publie le DNS et termine
TLS ; un tunnel dédié rejoint un proxy Nginx privé par socket Unix. Le proxy
redirige HTTP vers HTTPS et transmet l'origine HTTPS au CRM sur `127.0.0.1:3020`.
Les configurations et tunnels des autres applications restent inchangés.
Les fichiers et procédures sont dans [cloudflare/README.md](cloudflare/README.md).

Pour un diagnostic local, le tunnel SSH reste disponible :

```bash
ssh -N -L 127.0.0.1:3020:127.0.0.1:3020 VPS
```

La connexion utilise https://crm.avity.fr ; les cookies HTTP de localhost
ne sont pas réutilisés. Le compte administrateur et l'espace Avity-CRM existent
déjà. Les identifiants restent dans `/etc/avity-crm/admin.json` (root, mode 0600).
Le premier compte reçoit l'administration de l'instance. Le mode mono-espace et la restriction
aux administrateurs ferment la création libre de comptes/espaces.
Tester également le refus d'une nouvelle inscription non invitée.

`SHOULD_SEED_DEMO_DATA=false` évite les personnes, sociétés et opportunités
de démonstration ; les métadonnées, vues et modèles de workflow sont conservés.
Ce paramètre est une adaptation du fork, désactivée uniquement pour cette
installation. Aucun contact externe n'est importé. SMTP, fournisseurs de
messagerie/calendrier, télémétrie et synchronisation du catalogue sont désactivés.
Le driver e-mail `LOGGER` ne transmet pas de messages. Les logs et sauvegardes
doivent néanmoins rester privés, car les liens de connexion peuvent y figurer.

`SERVER_URL` est désormais `https://crm.avity.fr` sur le serveur et le worker.
L'image applicative, les secrets, les volumes et les données restent ceux de la
release installée. Le changement d'origine nécessite une nouvelle connexion,
sans reconstruction du frontend ni nouvel espace de travail.

## Vérifications

Le démarrage officiel initialise la base et exécute les upgrades sur le serveur ;
le worker ne les répète pas. Un `/healthz` vert ne suffit pas : l'entrypoint peut
continuer après un avertissement d'upgrade. Vérifier les journaux, les migrations,
les tables PostgreSQL, un travail réellement traité par le worker et
la connexion administrateur. Refaire la connexion et les contrôles de données
après redémarrage des quatre services.

```bash
sudo /opt/avity-crm/current/deploy/avity-crm/compose.sh exec -T server yarn database:migrate:prod --force --include-slow
sudo /opt/avity-crm/current/deploy/avity-crm/compose.sh exec -T server yarn command:prod upgrade
curl --fail http://localhost:3020/healthz
```

Ne pas utiliser `docker compose config` sans `--quiet` sur le fichier réel :
sa sortie développe les secrets.

## Sauvegarde et restauration

La couverture complète, le chiffrement, les rétentions, la reprise après erreur
et la planification préparée sont dans [BACKUP.md](BACKUP.md). Le snapshot inclut
sources/images durables et les cinq fichiers de publication. Ces nouveaux scripts
seront disponibles sur le VPS après une promotion autorisée ; aucune planification
de production n'est activée par cette PR.

```bash
sudo /opt/avity-crm/current/deploy/avity-crm/backup.sh
```

La sauvegarde suspend seulement les écrivains CRM initialement actifs, puis les
reprend sans recréer leurs conteneurs. Les snapshots privés sous
`/var/backups/avity-crm/` contiennent PostgreSQL, stockage, Redis, secrets,
administration, déploiement, sources/images durables et publication dédiée.
Seuls les snapshots vérifiés et marqués `COMPLETE` sont restaurables.

Tester d'abord la restauration dans une base et des volumes temporaires isolés.
Pour restaurer l'instance, après autorisation de perdre les écritures postérieures
à la sauvegarde :

1. Vérifier `SHA256SUMS`, le SHA source et la disponibilité de l'image sauvegardée.
2. Arrêter `server`, `worker` et `redis` avec le wrapper Compose.
3. Restaurer le fichier de secrets correspondant (mode 0600), le source et
   l'image correspondant au snapshot. Recréer le conteneur DB seulement si
   ses paramètres ont changé.
4. Supprimer puis recréer uniquement la base `avity_crm` vide, avec le
   propriétaire `avity_crm`, puis restaurer avec `pg_restore --no-owner
   --exit-on-error -U avity_crm -d avity_crm` via `compose.sh exec -T db`.
   Une simple restauration `--clean` laisserait des objets ajoutés après le
   snapshot et ne constitue pas un rollback fiable.
5. Remplacer le contenu des volumes CRM de stockage et de Redis avec leurs
   archives, tous les consommateurs arrêtés. Ne pas toucher aux autres volumes.
6. Démarrer avec `up -d --no-build --pull never`, puis vérifier santé, migrations,
   worker, connexion et données.

Ne pas restaurer uniquement la base en conservant les anciennes files Redis :
les jobs et fichiers doivent correspondre au même snapshot.

## Rollback

Avant chaque mise à jour, sauvegarder et conserver l'image/Compose du SHA actif.
Si les schémas sont compatibles, revenir au SHA et à l'image précédents puis
vérifier l'application. Si la mise à jour a modifié les schémas, utiliser le
snapshot complet correspondant après validation de la perte de données.
Ne pas supposer qu'une ancienne image peut lire une base migrée.

Lors de cette première installation, il n'existe pas de version CRM antérieure.
Un arrêt avec `compose.sh stop` garde les données pour une reprise avec le même
SHA. Ne pas utiliser `down -v`, ni un prune Docker global.
