# Accès public Avity-CRM par Cloudflare

URL : **https://crm.avity.fr**. Le compte administrateur existant utilise
désormais une session HTTPS avec cookie `__Host-twenty-session` sécurisé.

Le CNAME `crm.avity.fr` est proxifié vers
`f64f0461-f99f-4c92-8963-ca24deca5619.cfargotunnel.com`.
Le tunnel nommé `avity-crm` possède ses propres credentials et son unité
`cloudflared-avity-crm.service`. Aucun port entrant supplémentaire n'est ouvert.

Le chemin est Cloudflare → tunnel → socket privée
`/run/avity-crm-proxy/http.sock` → `127.0.0.1:3020` → serveur CRM.
L'unité `avity-crm-proxy.service` utilise le binaire Nginx déjà installé, avec
une configuration indépendante. HTTP est redirigé en 308 vers le même chemin et
les mêmes paramètres en HTTPS. HSTS s'applique au hostname CRM uniquement.
Le proxy conserve le streaming et les upgrades WebSocket.

## Configuration sur le VPS

- `/etc/cloudflared-avity-crm/config.yml` et `credentials.json` : root,
  groupe `cloudflared-avity-crm`, mode 0640 ; répertoire 0750.
- `/etc/avity-crm-proxy/nginx.conf` : root, groupe `avity-crm-ingress`,
  mode 0640 ; répertoire 0750.
- Les deux unités sont dans `/etc/systemd/system`, mode 0644, activées au boot.
- Le proxy utilise l'utilisateur `avity-crm-proxy`, groupe `avity-crm-ingress`.
  Le connecteur utilise `cloudflared-avity-crm` avec ce groupe supplémentaire
  pour accéder à la socket. Le proxy ne peut pas lire les credentials du tunnel.
- `/etc/avity-crm/avity-crm.env` conserve les secrets et le SHA de l'image ; seul
  `SERVER_URL` a changé vers `https://crm.avity.fr`. Aucun `FRONTEND_URL` distinct
  ni domaine personnalisé en base ne remplace cette origine.

Ce dossier contient les quatre configurations publiques. Les credentials
du tunnel ne doivent jamais être committés. Le certificat Cloudflare de gestion
du compte reste sur le poste local ; seuls les credentials de ce tunnel sont
nécessaires sur le VPS.

Après une restauration, recréer les utilisateurs/groupes et leurs permissions,
installer les fichiers sauvegardés aux chemins ci-dessus, puis vérifier :

```bash
sudo runuser -u avity-crm-proxy -- /usr/sbin/nginx -t -c /etc/avity-crm-proxy/nginx.conf
sudo runuser -u cloudflared-avity-crm -- /usr/local/bin/cloudflared --config /etc/cloudflared-avity-crm/config.yml tunnel ingress validate
sudo systemctl daemon-reload
sudo systemctl enable --now avity-crm-proxy.service cloudflared-avity-crm.service
curl --fail https://crm.avity.fr/healthz
curl --head http://crm.avity.fr/
```

`RuntimeDirectory` crée le répertoire de socket au démarrage du proxy. Avant un
test de syntaxe sur une machine neuve, créer `/run/avity-crm-proxy` avec propriétaire
`avity-crm-proxy:avity-crm-ingress`, mode 0750.

Une remise en route de ces unités ne doit pas redémarrer les autres services
Cloudflare ou le Nginx d'AvitySign. Conserver le CNAME existant si le tunnel est
restauré avec le même UUID ; ne pas remplacer un autre enregistrement DNS.

## Sauvegarde et rollback

La sauvegarde applicative est produite par `deploy/avity-crm/backup.sh`.
Conserver avec le même snapshot les quatre configurations de ce dossier,
les credentials privés du tunnel et `/etc/avity-crm/admin.json` ; inclure ces
fichiers dans `SHA256SUMS`, puis copier le snapshot hors du VPS en accès privé.
La sauvegarde post-publication contient `SERVER_URL=https://crm.avity.fr`.
Les anciennes sauvegardes localhost nécessitent de choisir explicitement
l'origine désirée après restauration des secrets.

Pour revenir à l'accès local, arrêter uniquement les deux nouvelles unités,
faire une copie privée de l'environnement courant, puis modifier uniquement
`SERVER_URL=http://localhost:3020` dans ce fichier courant. Recréer uniquement
serveur et worker avec la même image et les mêmes secrets. Le fichier initial
`/opt/avity-crm/qualification/cloudflare/avity-crm.env.before` sert de référence
pour l'ancienne origine ; ne pas le restaurer en bloc, car son SHA et ses secrets
pourraient être devenus anciens.
Retirer seulement le CNAME CRM si l'accès public doit être supprimé.
Ne pas restaurer la base ni supprimer de volume pour changer cette URL.

## Validation

Vérifier DNS proxifié, certificat valide sans exception TLS, redirection HTTP,
connexion de l'administrateur existant, session conservée après rafraîchissement,
liens workspace HTTPS, worker connecté et migrations à jour. Les POST avec cookie
et une origine absente ou étrangère doivent recevoir `403 CSRF_ORIGIN_MISMATCH`.
Comparer l'identifiant du workspace, son schéma, ses données et les références
d'images avant/après. Les autres DNS et configurations de tunnel doivent rester
inchangés.

Une ancienne réponse DNS négative peut rester dans le cache du poste après la
création du sous-domaine. Distinguer cette limite locale d'une panne du service
en vérifiant les serveurs DNS autoritaires et l'accès depuis le VPS.
