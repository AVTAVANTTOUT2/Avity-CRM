#!/usr/bin/env bash
set -euo pipefail

deployment_directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
repository_directory=$(git -C "$deployment_directory" rev-parse --show-toplevel)

if [[ -n $(git -C "$repository_directory" status --porcelain) ]]; then
  printf 'Build refused: commit the source tree before building.\n' >&2
  exit 1
fi

git_sha=$(git -C "$repository_directory" rev-parse HEAD)
image="avity-crm:git-$git_sha"
build_context=$(mktemp -d "${TMPDIR:-/tmp}/avity-crm-source.XXXXXX")
trap 'rm -rf -- "$build_context"' EXIT
git -C "$repository_directory" archive --format=tar "$git_sha" | tar -xf - -C "$build_context"

docker build --platform linux/amd64 --target twenty \
  --file "$build_context/packages/twenty-docker/twenty/Dockerfile" \
  --build-arg APP_VERSION=v2.45.0 \
  --label org.opencontainers.image.source=https://github.com/AVTAVANTTOUT2/Avity-CRM \
  --label "org.opencontainers.image.revision=$git_sha" \
  --label org.opencontainers.image.version=v2.45.0 \
  --tag "$image" "$build_context"

[[ $(docker image inspect --format '{{.Architecture}}' "$image") == amd64 ]]
[[ $(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image") == "$git_sha" ]]
docker run --rm --network none --entrypoint sh "$image" -c \
  'test -s dist/front/index.html && test -s dist/main.js && test -s dist/queue-worker/queue-worker.js'
printf 'Built %s\n' "$image"
