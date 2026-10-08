# Promotion future et retour arrière

Cette PR ne fusionne ni ne déploie en production. Sa base `deploy/crm-cloudflare`
contient les adaptations réellement installées ; Twenty reste en version 2.45.0.
Une nouvelle autorisation est requise pour exécuter cette promotion.

1. Choisir le SHA propre approuvé. Construire hors VPS avec **Avity CRM image**,
   vérifier CI, checksum, AMD64 et label OCI. Serveur et worker utilisent la même
   image `avity-crm:git-SHA`. Qualifier ce SHA en staging, y compris restauration,
   worker, migrations, sécurité, login/logout/session et affichage.
2. Après autorisation, revérifier disque/RAM et autres services. Installer le
   nouveau code dans `/opt/avity-crm/releases/SHA` en conservant l'ancienne release.
   Installer le source exact `/opt/avity-crm/artifacts/SHA/source.tar.gz`, conserver
   l'image exportée durablement, la charger après checksum. Préserver secrets,
   admin, DNS, tunnel, Nginx et unités existantes.
3. Faire une sauvegarde complète de l'état actif avec les nouveaux scripts et les
   chemins actifs explicitement sélectionnés. Prévoir la courte suspension des
   seuls écrivains CRM. Vérifier copie externe et restauration avant activation.
4. Modifier seulement `GIT_SHA` dans le fichier privé sauvegardé ; conserver port
   3020 et `SERVER_URL=https://crm.avity.fr`. Basculer atomiquement `current`, puis
   lancer le nouveau wrapper `compose.sh up -d --no-build --pull never --wait`
   pour `avity-crm`. Vérifier migrations/upgrades, worker, HTTPS, login et données.
   Comparer l'état des autres applications. Le branding ne demande aucun changement
   Cloudflare/Nginx.

La planification demande une activation distincte après choix réel d'une clé
publique et d'un montage hors VPS vérifié, conservation séparée de la clé privée,
test de déchiffrement/restauration et vérification disque/rétention. Les paramètres
et unités sont préparés dans `schedule/`. Le montage/destinataire synthétiques du
Mac ne valent pas externalisation automatique de production.

## Rollback

Si les schémas restent compatibles, arrêter les écrivains CRM, remettre le
`GIT_SHA` et le lien `current` précédents, reprendre l'image précédente conservée,
puis vérifier données/login/worker. Conserver secrets et publication actifs.

Si les schémas sont incompatibles, restaurer ensemble base fraîche, stockage,
Redis, secrets, images et configuration du snapshot correspondant, après accord
explicite sur la perte des écritures postérieures. Suivre [BACKUP.md](BACKUP.md).
Ne pas supposer qu'une ancienne image lit une base migrée. Aucun Compose upstream,
`down -v` de production sans autorisation ou prune global.
