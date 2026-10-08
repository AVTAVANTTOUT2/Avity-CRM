#!/usr/bin/env bash
set -euo pipefail

deployment_directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
environment_file=${AVITY_CRM_ENV_FILE:-/etc/avity-crm/avity-crm.env}
project=${AVITY_CRM_PROJECT:-avity-crm}
compose_files=(--file "$deployment_directory/compose.yml")

if [[ $# == 0 || $1 == -* ]]; then
  printf 'Pass a Compose command; global configuration overrides are forbidden.\n' >&2
  exit 1
fi
skip_option_value=0
for argument in "${@:2}"; do
  # Exec/run stop parsing flags at the service name; child arguments stay intact.
  case "$argument" in
    --project-name|--project-name=*|-p*|--file|--file=*|-f*|--env-file|--env-file=*|--project-directory|--project-directory=*)
      printf 'Compose project and configuration overrides are forbidden.\n' >&2; exit 1 ;;
  esac
  if [[ $1 == exec || $1 == run ]]; then
    if [[ $skip_option_value == 1 ]]; then
      skip_option_value=0
      continue
    fi
    case "$argument" in
      --user|-u|--workdir|-w|--env|-e|--index|--entrypoint|--name|--label|-l|--volume|-v|--publish)
        skip_option_value=1 ;;
      server|worker|db|redis|gateway) break ;;
    esac
  fi
done

case "$project" in
  avity-crm) ;;
  avity-crm-staging|avity-crm-staging-restore)
    if [[ -z ${AVITY_CRM_ENV_FILE:-} ]]; then
      printf 'Staging requires its own explicit environment file.\n' >&2
      exit 1
    fi
    python3 "$deployment_directory/staging-guard.py" "$environment_file"
    compose_files+=(--file "$deployment_directory/staging/compose.yml")
    ;;
  *) printf 'Unsupported CRM project.\n' >&2; exit 1 ;;
esac

# Compose gives the shell precedence over --env-file; the validated file is authoritative.
unset GIT_SHA HTTP_PORT SERVER_URL PG_DATABASE_PASSWORD APP_SECRET ENCRYPTION_KEY
exec docker compose --project-name "$project" \
  --env-file "$environment_file" \
  "${compose_files[@]}" "$@"
