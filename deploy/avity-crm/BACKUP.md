# Sauvegarde complète et restauration

Ce lot prépare le nouveau mécanisme ; il ne l'installe et ne le planifie pas en
production. Les essais utilisent seulement des données synthétiques du staging.

## Couverture et cohérence

`backup.sh` garde le projet `avity-crm` par défaut. Le staging sélectionne
explicitement ses fichiers via `staging.sh backup`. Un verrou non bloquant refuse
deux exécutions simultanées. Le répertoire est privé dès sa création.

Chaque snapshot contient :

| Fichier | Contenu |
| --- | --- |
| `database.dump` | PostgreSQL au format custom, table des matières vérifiée |
| `storage.tar.gz`, `redis.tar.gz` | Stockage applicatif et Redis, RDB/AOF cohérents |
| `avity-crm.env`, `admin.json` | Secrets de l'application et administration, mode 0600 |
| `deployment.tar.gz` | Compose, scripts, configurations et documentation réellement utilisés |
| `source.tar.gz` | Archive Git durable ; son commentaire PAX doit être le SHA applicatif |
| `images.tar.gz`, `images.json` | Couches réellement présentes des images actives, sauvegardées par ID immuable |
| `service-state.json` | Services initialement actifs, écrit et fsync avant l'arrêt |
| `manifest.json`, `SHA256SUMS`, `COMPLETE` | Inventaire version 1, tailles/SHA256 et marqueur final |

La production exige aussi cinq fichiers de publication : configuration et
credential **du tunnel CRM uniquement**, Nginx et les deux unités systemd.
Les chemins sont `/etc/cloudflared-avity-crm/{config.yml,credentials.json}`,
`/etc/avity-crm-proxy/nginx.conf` et les unités CRM de `/etc/systemd/system`.
Les unités 0644 sont acceptées ; leur copie devient 0600. Aucun `cert.pem` de
gestion du compte Cloudflare n'est sauvegardé.

Les sources viennent de `/opt/avity-crm/artifacts/<SHA>/source.tar.gz`, ou de
`AVITY_CRM_ARTIFACTS_DIRECTORY`. Conserver cette archive **avant** une mise à jour.
Les fichiers déployés et les configurations privées ont leurs propres sommes
de contrôle : le SHA applicatif peut différer de celui de leur installation.
Les images et sources sont incluses dans le snapshot ; la restauration ne
dépend plus de la rétention de 30 jours des artefacts Actions.

Avant l'arrêt : vérifier les fichiers, le SHA et les montages de tous les services,
puis sauvegarder les images. Suspendre seulement les serveur/worker initialement
actifs, exécuter Redis SAVE puis arrêter Redis s'il était actif. PostgreSQL reste
actif pour le dump. Archiver les volumes sans écrivain, vérifier les sommes,
archives complètes et couches/configs/indexes Docker/OCI, puis reprendre seulement
les services initialement actifs avec `--no-recreate`. Marquer `COMPLETE` en dernier.

```bash
sudo deploy/avity-crm/backup.sh
sudo python3 deploy/avity-crm/verify-backup.py /var/backups/avity-crm/SNAPSHOT
```

Ne jamais publier un snapshot, une capsule déchiffrée ou un `.env` en CI/PR.
Ne jamais afficher le Compose développé ou la sortie d'un credential.

## Échecs et interruptions

Un échec garde `SNAPSHOT.incomplete/FAILED.json` avec étape, code et résultat de
reprise. Aucune réussite ni rétention n'est annoncée. HUP/INT/TERM interrompent le
processus externe puis attendent la reprise. Le job planifié relaie ces signaux.
Prévoir jusqu'à 3 × 180 secondes de reprise ; l'unité accorde 10 minutes à l'arrêt.

SIGKILL, coupure électrique, Docker inaccessible ou disque plein peuvent empêcher
la réparation automatique. Lire **le fichier privé** `service-state.json` de
l'essai incomplet, vérifier les IDs/montages, puis reprendre seulement les services
énumérés dans ce fichier. Ne pas lancer un `up` global si un service était arrêté
volontairement. Un snapshot incomplet n'est jamais une entrée de restauration.
Les essais incomplets et anciennes sauvegardes au format historique sont conservés
pour diagnostic ; leur nettoyage et la capacité disque doivent être surveillés.

## Chiffrement, copie et rétention

`backup-job.sh` vérifie l'empreinte et la capacité réelle de chiffrement d'une
**clé publique dédiée**, ainsi que l'identité du montage hors hôte, avant de
lancer la sauvegarde. `export-backup.py SNAPSHOT` peut exporter un snapshot déjà
validé. GnuPG chiffre la capsule complète avec AES256 pour ce destinataire.
La clé privée de déchiffrement doit rester ailleurs et être sauvegardée séparément.
Une clé privée fournie au script est refusée. Aucun secret ne passe en argument.

Les paramètres sans valeur par défaut de destination figurent dans
[schedule/backup.env.example](schedule/backup.env.example). Le montage existant
doit être déclaré par `SOURCE`, `FSTYPE` et `TARGET` exacts, obtenus via
`findmnt --json --target DESTINATION --output SOURCE,FSTYPE,TARGET`.
NFS/CIFS/SSHFS/virtiofs sont pris en charge. Un dossier local laissé par un montage
absent est refusé, avant/après copie. La racine peut être 0755, mais pas inscriptible
par d'autres utilisateurs ; le namespace du projet est 0700.

La copie passe par `.incomplete`, est fsync puis relue pour vérifier son SHA256.
Un reçu `.verified.json` enregistre le checksum, la taille, le projet, le SHA et
l'empreinte publique. Une copie chiffrée doit aussi être déchiffrée et restaurée
périodiquement : le reçu seul ne prouve pas la restauration.

`AVITY_CRM_BACKUP_KEEP_COUNT=14` conserve les exports vérifiés du même projet.
`AVITY_CRM_BACKUP_LOCAL_KEEP_COUNT=2` conserve les snapshots complets locaux.
Les deux rétentions n'opèrent qu'après succès de la nouvelle copie chiffrée ; elles
ignorent les autres projets, liens, fichiers inconnus, échecs et formats historiques.
Les chemins du stockage de production ne sont jamais autorisés au staging.

L'essai local exporte de la VM dédiée vers un sous-dossier du stockage de sauvegardes
existant sur le Mac. **Cela ne constitue pas une copie indépendante de ce Mac ni
une externalisation automatique de production.** Le destinataire de test et sa clé
privée ne doivent pas être réutilisés en production.

## Planification préparée

[schedule/avity-crm-backup.timer](schedule/avity-crm-backup.timer) programme 03:50 UTC,
avec décalage aléatoire maximal 10 minutes et rattrapage après arrêt. Son service
utilise `/etc/avity-crm/backup.env` privé, Nice 10 et priorité I/O basse.
L'installation future des unités et `systemctl enable --now` exigent une nouvelle
autorisation de production. **Aucune unité de sauvegarde de production n'est
installée ni activée par cette PR.** Vérifier capacité disque, montage durable,
clé publique, déchiffrement et procédure de restauration avant l'activation.

## Qualification complète isolée

Déchiffrer la capsule avec la clé privée extérieure, vers un emplacement privé,
en passant un chemin `--output` à GPG ; jamais de dump sur stdout. Vérifier son reçu
SHA256 avant déchiffrement. Puis sur l'hôte de test :

```bash
python3 deploy/avity-crm/unpack-backup.py /PRIVATE/snapshot.tar /PRIVATE/new-input
sudo python3 deploy/avity-crm/restore-staging.py /PRIVATE/new-input/SNAPSHOT \
  --port 3022 --confirm-restore-test-data-loss
```

Le restaurateur accepte uniquement un snapshot du staging initialement actif et
cible **uniquement** `avity-crm-staging-restore`, dans
`/var/lib/avity-crm-staging-restore`. Le consentement explicite autorise la suppression
des seuls volumes de ce projet. Il recharge les images depuis la capsule, restaure
la base fraîche, remplace stockage/Redis et reprend l'application. Le port est lié
à 127.0.0.1 ; les secrets du snapshot sont préservés, seule l'origine locale change.
Les fichiers de publication sont restaurés dans une fixture privée, jamais dans `/etc`.
Aucun tunnel n'est lancé. Vérifier login, fiche synthétique, marqueurs fichier/Redis,
worker et migrations ; comparer les checksums des fichiers de publication.

## Restauration publique et rollback futurs

Une restauration publique reste **NON EXÉCUTÉE par ce lot**. Après autorisation et
acceptation de la perte des écritures postérieures au snapshot : vérifier/déchiffrer
la capsule ; installer sources/scripts/images associés ; restaurer les secrets 0600 ;
arrêter les écrivains CRM ; recréer seulement sa base vide et remplacer ses seuls
volumes stockage/Redis. Restaurer les cinq fichiers de publication avec leurs
droits/groupes de service, tester Nginx et `cloudflared tunnel ingress validate`
sans connecter le tunnel, puis reprendre les unités dédiées. Ne pas modifier le
DNS si le tunnel sauvegardé reste valide. Un tunnel supprimé côté Cloudflare
nécessite une procédure de récupération distincte, pas seulement un fichier local.

Le rollback complet remet ensemble images, données, fichiers, Redis, secrets et
configuration du snapshot. Un retour d'image seul est possible seulement si les
schémas sont compatibles ; ne pas supposer qu'une ancienne image lit une base migrée.
Le détail de promotion figure dans [DEPLOYMENT.md](DEPLOYMENT.md).
