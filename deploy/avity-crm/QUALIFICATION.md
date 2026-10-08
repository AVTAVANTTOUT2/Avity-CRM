# Qualification du socle Avity-CRM

Essais du 8 octobre 2026. La production est restée en lecture seule.
Base de PR : `deploy/crm-cloudflare` (`ce619da739d0a55a295573d5d8739ebd40ac14bc`).
Branche : `feat/avity-crm-foundation`. Twenty reste à `twenty/v2.45.0`,
upstream `7e1431c84adbb264db98ab8b8d008a8401b6d4d6`.

## Sources et image qualifiées

Les captures et contrôles applicatifs ci-dessous portent sur
`bc05c4c26e5d29977b3165523fa4679774939784`, construit depuis son archive Git vierge.
La [CI de ce SHA](https://github.com/AVTAVANTTOUT2/Avity-CRM/actions/runs/37811370167)
est **PASS**. Serveur et worker utilisent la même image AMD64
`avity-crm:git-bc05c4c26e5d29977b3165523fa4679774939784`.
Son ID OCI dans Docker 29 est
`sha256:bce097685b78dade7e99f372684345a38b117f344d4416a4068efd66a7ec84ce`.
Le checksum de l'export Actions et son label `revision` ont été vérifiés.
L'ID de configuration Docker 28 dans l'artefact diffère de l'ID OCI Docker 29 ;
ils ne désignent pas le même objet de stockage.

L'arbre Git frontend est `fe6865f3ba30a21f62c1848baa5ed85e54f881d8`.
Les preuves sont commitées après leur capture. Les références de l'image et de la
CI du HEAD final installé sont consignées dans la PR ; elles doivent être vérifiées
séparément des références historiques de cette page.

## Résultats

| Contrôle | Statut et preuve |
| --- | --- |
| Construction officielle complète interface/serveur | PASS, CI du SHA ci-dessus, cible `twenty`, exécution des fichiers de distribution |
| Typage frontend complet | PASS, `tsgo --noEmit` dans l'environnement verrouillé de construction |
| Lint / formatage des fichiers frontend modifiés | PASS, oxlint sans avertissement ni erreur, oxfmt |
| Tests frontend | PASS, 2 suites / 4 tests : titres traduits, suffixe par défaut, URL du logo public |
| Sauvegarde / staging / chiffrement-export / restauration | PASS, respectivement 59 / 31 / 25 / 23 tests, soit 138 |
| Compatibilité du restaurateur | PASS, 23 tests avec Compose 2.38.2 puis 5.1.4 ; refus des `env_file` externes avant résolution |
| Échecs et reprise des services | PASS : concurrence, fichier obligatoire absent, archive invalide, signaux INT/TERM, état initial préservé |
| Connexion, rafraîchissement, navigation, déconnexion | PASS en Chromium sur l'image qualifiée ; accès métier redirigé après logout |
| Accès anonyme, inscription non invitée, CSRF cookies | PASS, refus attendus ; origine absente ou étrangère rejetée avec HTTP 403 |
| Fiche fictive | PASS, création, modification, lecture et persistance de `AVITY-STAGING-SYNTHETIC-BACKUP-A-EDITED` |
| Base et upgrades | PASS, 182 migrations TypeORM appliquées, workspace à jour, dépendances opérationnelles |
| Worker réel | PASS, `GenerateSdkClientJob` de `workspace-queue` terminé ; serveur/worker sans redémarrage involontaire |
| Redémarrage isolé | PASS, reprise des cinq services, fiche et marqueurs fichier/Redis `SYNTHETIC-A` conservés |
| Isolation réseau | PASS, réseau app/worker interne, connexion TCP externe refusée, DB/Redis sans port publié |
| Identité et lisibilité | PASS, logo chargé avec HTTP 200, titres/manifest/favicon, clair/sombre, ordinateur et mobile, chargement métadonnées |
| Langue / échelle | PASS sur le staging : français puis anglais, zoom UI 125 % puis 100 %, aucun débordement horizontal de la page |
| Erreurs navigateur | PASS, zéro `pageerror` pendant le parcours de qualification |
| Revue indépendante | PASS après correction des défauts démontrés de restauration et de résolution du logo |

Une tentative de contrôles frontend dans le checkout Mac a **FAIL d'environnement**
(distributions `twenty-shared` et catalogues Lingui absents). Elle ne constitue pas
une preuve de validation locale. Les contrôles complets équivalents ont ensuite
passé dans la CI utilisant les dépendances déjà verrouillées ; aucun package ni
lockfile produit n'a été modifié pour masquer cet échec.

Les deux défauts visuels trouvés en staging sont corrigés : priorité CSS du bouton
d'accent et URL du logo par défaut. Le vrai résolveur d'image a aussi été exercé
avant/après : rouge sur `e7309c27`, vert sur le SHA qualifié, avec origines frontend
et serveur différentes. Les URLs de logos téléversés gardent leur comportement.
Les contrastes calculés du bouton d'accent sont 6,73 en clair et 7,58 en sombre ;
ce contrôle ciblé n'est pas un audit d'accessibilité complet.

## Sauvegarde et restauration réellement exercées

Le snapshot synthétique `20261008T143901Z-2a178aaa`, image de référence `ec8a5326`,
a été vérifié, chiffré, copié hors VM avec relecture du checksum, déchiffré avec
la clé privée conservée sur le Mac, puis restauré dans **un second projet**.
Après mutation de l'original vers un état B, la restauration a retrouvé l'état A
de la fiche, du stockage et de Redis, ainsi que les secrets, l'administration,
les sources/images et les cinq fichiers de publication. Le restaurateur renforcé
a été réexercé sur cette capsule. Les résultats complémentaires sur les images
candidates sont indiqués dans la PR.

La couverture exacte et les commandes reproductibles sont dans [BACKUP.md](BACKUP.md).
PostgreSQL, stockage, Redis, secrets de déchiffrement, administration, déploiement,
sources/images durables, état des services et publication CRM dédiée sont couverts.
Les cinq fichiers Cloudflare/Nginx/systemd utilisés pour les essais sont des fixtures
fictives : checksums identiques après restauration, `nginx -t` et validation locale
des ingress Cloudflare passent. Aucun tunnel n'a été connecté pour ces validations.

| Fonction | État effectif |
| --- | --- |
| Chiffrement et déchiffrement | TESTÉ, clé dédiée synthétique ; clé privée hors VM et hors capsule |
| Copie chiffrée et vérification | TESTÉ, VM vers le stockage Mac existant, reçu SHA256 et taille vérifiés |
| Rétention | TESTÉ, suppression d'un ancien snapshot complet après nouvel export réussi, échecs/archives étrangères préservés |
| Déclenchement planifié | TESTÉ par timers systemd ponctuels de staging, dont un échec sans démarrage intempestif puis un succès complet |
| Sauvegarde quotidienne production | PRÉPARÉE, NOT RUN : aucune nouvelle unité installée/activée en production |
| Externalisation automatique production | BLOCKED avant activation : destination hors VPS et destinataire public de production à configurer |
| Copie indépendante du Mac | NOT RUN : la copie hors VM reste sur ce même ordinateur |
| Restauration publique / promotion / rollback production | NOT RUN, hors autorisation de ce lot |

La clé synthétique et le montage de test ne constituent pas une configuration de
production. La [promotion et le rollback](DEPLOYMENT.md) nécessitent une autorisation
distincte et la vérification de ces prérequis.

## Accès et captures

VM locale Colima `crm-foundation`, contexte `colima-crm-foundation`, projet
`avity-crm-staging`, état privé `/var/lib/avity-crm-staging`.
Accès : **http://localhost:3021** sur le Mac, uniquement loopback. Les identifiants
restent privés dans `admin.json` de cette VM. Voir [STAGING.md](STAGING.md) pour
démarrage, état, tests, arrêt et reset limité au staging. Le second projet de
restauration utilise 3022 et reste arrêté après qualification.

Avant : image de référence `ec8a5326` sur le projet restauré, données fictives.
Après : image qualifiée `bc05c4c2` sur le staging, sans injection de styles.
Ordinateur 1440 × 1000 ; mobile 390 × 844. Les champs d'authentification sont vides.
Pour les loaders, `FindMinimalMetadata` est retenue dans un contexte authentifié
sans cache IndexedDB ; le loader de métadonnées se distingue du squelette de route.

| Écran | Avant | Après |
| --- | --- | --- |
| Connexion | [capture](proofs/before-login.png) | [capture](proofs/after-login.png) |
| Navigation / clair | [capture](proofs/before-desktop.png) | [capture](proofs/after-desktop.png) |
| Sombre | [capture](proofs/before-dark.png) | [capture](proofs/after-dark.png) |
| Mobile | [capture](proofs/before-mobile.png) | [capture](proofs/after-mobile.png) |
| Chargement ordinateur | [capture](proofs/before-loading.png) | [capture](proofs/after-loading.png) |
| Chargement mobile | [capture](proofs/before-loading-mobile.png) | [capture](proofs/after-loading-mobile.png) |

Les points Avity à surveiller lors d'un upgrade sont dans [BRANDING.md](BRANDING.md).
Licences, mentions d'origine, identifiants internes et mécanismes d'auth/session/CSRF
sont conservés ; SMTP, calendriers et télémétrie restent désactivés.

## Production : comparaison en lecture seule

**PASS pour le CRM** : lien `current`, sept fichiers privés/configurations,
quatre conteneurs (IDs, images, heures de démarrage, compteurs de restart) et
deux unités dédiées (PID/date d'activation) inchangés. L'image active reste
`avity-crm:git-ec8a5326514eed20c17e646e5a688e17dfb76e44`.
Le checkout principal et le contexte Docker par défaut sont préservés.

Parmi les autres tunnels observés, `cloudflared-preprod.service` a redémarré à
14:15:28 UTC pendant la fenêtre de comparaison ; les trois autres unités suivies
sont inchangées. Aucune commande de ce lot n'a modifié ce tunnel ni les autres
applications. Cette observation empêche d'affirmer que tout l'hôte est resté figé.
Aucun secret, dump, snapshot ou credential n'est ajouté à Git ou aux artefacts CI.
