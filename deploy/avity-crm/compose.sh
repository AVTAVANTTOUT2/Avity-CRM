#!/usr/bin/env bash
set -euo pipefail

deployment_directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
environment_file=${AVITY_CRM_ENV_FILE:-/etc/avity-crm/avity-crm.env}

exec docker compose --project-name avity-crm \
  --env-file "$environment_file" \
  --file "$deployment_directory/compose.yml" "$@"
