#!/usr/bin/env bash
set -euo pipefail
umask 077

directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
state=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "${AVITY_CRM_STAGING_ROOT:-/var/lib/avity-crm-staging}")
export AVITY_CRM_PROJECT=avity-crm-staging
export AVITY_CRM_ENV_FILE="$state/avity-crm.env"

if [[ $(uname -s) != Linux || $EUID != 0 ]]; then
  printf 'Run in the dedicated Linux test host as root.\n' >&2
  exit 1
fi
case "$state" in
  /etc/avity-crm|/etc/avity-crm/*|/opt/avity-crm|/opt/avity-crm/*|/var/backups/avity-crm|/var/backups/avity-crm/*)
    printf 'Production paths are forbidden for staging.\n' >&2; exit 1 ;;
esac

command=${1:-status}
case "$command" in
  init)
    git_sha=${2:-$(git -C "$directory" rev-parse HEAD)}
    python3 - "$state" "$git_sha" "${AVITY_CRM_STAGING_PORT:-3021}" <<'PY'
import base64, json, pathlib, re, secrets, socket, sys
root = pathlib.Path(sys.argv[1]).resolve()
sha, port = sys.argv[2], int(sys.argv[3])
if not re.fullmatch(r'[0-9a-f]{40}', sha) or port == 3020 or not 1024 <= port <= 65535:
    sys.exit('Invalid staging SHA or port.')
with socket.socket() as sock:
    sock.bind(('127.0.0.1', port))
root.mkdir(mode=0o700, parents=True, exist_ok=True)
root.chmod(0o700)
if (root / 'avity-crm.env').exists() or (root / 'admin.json').exists():
    sys.exit('Staging already initialized; existing credentials preserved.')
values = dict(GIT_SHA=sha, HTTP_PORT=str(port), SERVER_URL=f'http://localhost:{port}',
              PG_DATABASE_PASSWORD=secrets.token_hex(32), APP_SECRET=secrets.token_hex(32),
              ENCRYPTION_KEY=base64.b64encode(secrets.token_bytes(32)).decode())
with (root / 'avity-crm.env').open('x') as file:
    file.write(''.join(f'{key}={value}\n' for key, value in values.items()))
with (root / 'admin.json').open('x') as file:
    json.dump(dict(email='admin@staging.avity.invalid', password=secrets.token_urlsafe(32),
                   firstName='Avity', lastName='Staging'), file)
print('Staging initialized with separate synthetic credentials.')
PY
    ;;
  up)
    python3 "$directory/staging-guard.py" "$AVITY_CRM_ENV_FILE"
    git_sha=$(sed -n 's/^GIT_SHA=//p' "$AVITY_CRM_ENV_FILE")
    revision=$(docker image inspect "avity-crm:git-$git_sha" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')
    [[ "$revision" == "$git_sha" ]] || { printf 'Candidate image revision mismatch.\n' >&2; exit 1; }
    "$directory/compose.sh" up -d --no-build --pull never --wait --wait-timeout 360
    ;;
  status) "$directory/compose.sh" ps ;;
  stop) "$directory/compose.sh" stop ;;
  backup)
    export AVITY_CRM_ADMIN_FILE="$state/admin.json"
    export AVITY_CRM_BACKUP_ROOT="$state/backups"
    export AVITY_CRM_BACKUP_LOCK="$state/backup.lock"
    export AVITY_CRM_ARTIFACTS_DIRECTORY="$state/artifacts"
    export AVITY_CRM_PUBLICATION_BACKUP=${AVITY_CRM_PUBLICATION_BACKUP:-0}
    exec "$directory/backup.sh"
    ;;
  reset)
    [[ ${2:-} == --confirm-staging-data-loss ]] || { printf 'Pass --confirm-staging-data-loss to delete staging volumes only.\n' >&2; exit 1; }
    "$directory/compose.sh" down --volumes --remove-orphans
    ;;
  *) printf 'Use init [SHA], up, status, stop, backup or reset --confirm-staging-data-loss.\n' >&2; exit 1 ;;
esac
