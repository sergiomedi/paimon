#!/usr/bin/env bash
# Build the API image and push it to this environment's registry.
#
#   ./scripts/azure/publish.sh              build and tag with the current commit
#   ./scripts/azure/publish.sh v3           build and tag v3
#
# Runs before deploy.sh the first time, because a Container App whose image does
# not exist is an app that deploys and never starts. After that, run it whenever
# the code changes and deploy again: deploy.sh picks up the newest tag.
#
# The build happens in Azure, not here. `az acr build` uploads the build context
# and runs the Dockerfile on ACR's own agents, which means this works from a
# machine with no Docker daemon — including WSL without Docker Desktop, and
# including CI — and the image never crosses the network as layers.

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

require_az
announce

REGISTRY="$(registry_name)"
[[ -n "$REGISTRY" ]] || die "no registry in ${GROUP}. Deploy the environment first: ./scripts/azure/deploy.sh"

# The commit, so that a deployed environment can be traced back to a tree. The
# suffix marks a build made from uncommitted changes, which is a thing worth
# knowing when the deployed behaviour does not match the repository.
if [[ -n "${1:-}" ]]; then
    TAG="$1"
else
    TAG="$(git -C "$ROOT" rev-parse --short HEAD)"
    git -C "$ROOT" diff --quiet HEAD 2>/dev/null || TAG="${TAG}-dirty"
fi

IMAGE="${IMAGE_REPOSITORY}:${TAG}"

bold "▸ building ${IMAGE} in ${REGISTRY}"
printf 'Context is the repository root: the Dockerfile copies from backend/, and\n'
printf '.dockerignore keeps git history, virtual environments and caches out of it.\n\n'

az acr build \
    --registry "$REGISTRY" \
    --image "$IMAGE" \
    --file "$ROOT/docker/backend.Dockerfile" \
    "$ROOT"

printf '\n'
bold "▸ published"
printf 'Image  %s.azurecr.io/%s\n\n' "$REGISTRY" "$IMAGE"
printf 'deploy.sh will pick this up on its own. To pin it explicitly:\n'
printf '  export PAIMON_API_IMAGE=%s.azurecr.io/%s\n' "$REGISTRY" "$IMAGE"
