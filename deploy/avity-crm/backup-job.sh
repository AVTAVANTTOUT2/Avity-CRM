#!/usr/bin/env bash
set -euo pipefail
umask 077
directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
exec python3 "$directory/backup-job.py"
