#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $EUID != 0 ]]; then
  printf 'Run this backup as root.\n' >&2
  exit 1
fi

deployment_directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
environment_file=${AVITY_CRM_ENV_FILE:-/etc/avity-crm/avity-crm.env}
backup_root=${AVITY_CRM_BACKUP_ROOT:-/var/backups/avity-crm}
lock_file=${AVITY_CRM_BACKUP_LOCK:-/run/lock/avity-crm-backup.lock}
backup_directory="$backup_root/$(date -u +%Y%m%dT%H%M%SZ)"
compose="$deployment_directory/compose.sh"

exec 9>"$lock_file"
flock -n 9 || { printf 'Another CRM backup is running.\n' >&2; exit 1; }
mkdir -p "$backup_directory"

service_listing=$("$compose" ps --status running --services)
running_services=()
while IFS= read -r service; do
  case "$service" in
    server|worker|redis) running_services+=("$service") ;;
  esac
done <<< "$service_listing"

resume_services() {
  local service running_service
  for service in redis server worker; do
    for running_service in "${running_services[@]}"; do
      if [[ $service == "$running_service" ]]; then
        "$compose" up -d --no-build --pull never --no-deps \
          --wait --wait-timeout 180 "$service" || return 1
      fi
    done
  done
}
finish_backup() {
  local result=$?
  trap - EXIT
  if ! resume_services; then
    printf 'Backup finished but service recovery failed; inspect CRM containers.\n' >&2
    exit 1
  fi
  exit "$result"
}
trap finish_backup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

"$compose" stop server worker
"$compose" exec -T redis redis-cli SAVE >/dev/null
"$compose" stop redis
"$compose" exec -T db sh -c \
  'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom' \
  > "$backup_directory/database.dump"

cp "$environment_file" "$backup_directory/avity-crm.env"
cp "$deployment_directory/compose.yml" "$backup_directory/compose.yml"
cp "$deployment_directory/compose.sh" "$backup_directory/compose.sh"
cp "$deployment_directory/backup.sh" "$backup_directory/backup.sh"
"$compose" images --format json > "$backup_directory/images.json"

database_image=$("$compose" images -q db)
for entry in server-local-data:storage redis-data:redis; do
  volume_name="avity-crm_${entry%%:*}"
  archive_name="${entry#*:}.tar.gz"
  docker run --rm --network none --read-only \
    --mount "type=volume,src=$volume_name,dst=/data,readonly" \
    --mount "type=bind,src=$backup_directory,dst=/backup" \
    --entrypoint tar "$database_image" \
    -czf "/backup/$archive_name" -C /data .
done

(
  cd "$backup_directory"
  sha256sum database.dump storage.tar.gz redis.tar.gz avity-crm.env \
    compose.yml compose.sh backup.sh images.json > SHA256SUMS
)
chmod -R go-rwx "$backup_directory"
printf 'Backup created: %s\n' "$backup_directory"
