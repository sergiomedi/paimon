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
# Two ways to build, tried in that order.
#
# `az acr build` uploads the context and runs the Dockerfile on ACR's own agents,
# so it needs no Docker daemon here and the image never crosses the network as
# layers. It is the better one and it is tried first.
#
# It does not work on a trial subscription. Microsoft suspended ACR Tasks runs
# funded by Azure free credits — `TasksOperationsNotAllowed`, with an official
# answer of "use a pay-as-you-go subscription" that has stood since 2024. So when
# Azure refuses for that reason, this falls back to building with the local Docker
# daemon and pushing. Same image, same tag, different machine doing the work.
#
# The fallback pins --platform linux/amd64. Container Apps runs amd64, a local
# build takes the host's architecture, and an arm64 image pushed from a Mac
# deploys perfectly and then fails at startup with an exec format error that
# mentions nothing about architecture.

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

REFERENCE="${REGISTRY}.azurecr.io/${IMAGE}"

build_in_azure() {
    az acr build \
        --registry "$REGISTRY" \
        --image "$IMAGE" \
        --file "$ROOT/docker/backend.Dockerfile" \
        "$ROOT" 2>&1
}

build_here() {
    command -v docker >/dev/null 2>&1 ||
        die "no local Docker either. Install one, or build in CI and push to ${REGISTRY}.azurecr.io."
    docker info >/dev/null 2>&1 ||
        die "Docker is installed but not running. Start it and try again."

    bold "▸ logging in to ${REGISTRY}"
    az acr login --name "$REGISTRY"

    bold "▸ building ${REFERENCE} locally"
    docker build \
        --platform linux/amd64 \
        --tag "$REFERENCE" \
        --file "$ROOT/docker/backend.Dockerfile" \
        "$ROOT"

    bold "▸ pushing"
    docker push "$REFERENCE"
}

if OUTPUT=$(build_in_azure); then
    printf '%s\n' "$OUTPUT"
elif [[ "$OUTPUT" == *TasksOperationsNotAllowed* ]]; then
    printf '\n'
    warn "Azure refused to build: ACR Tasks are not permitted on this subscription."
    warn "Microsoft suspended task runs funded by free credits; the official answer is"
    warn "a pay-as-you-go subscription. Building here instead — same image, same tag."
    printf '\n'
    build_here
else
    printf '%s\n' "$OUTPUT"
    die "the build failed."
fi

printf '\n'
bold "▸ published"
printf 'Image  %s\n\n' "$REFERENCE"
printf 'deploy.sh will pick this up on its own. To pin it explicitly:\n'
printf '  export AZURE_PAIMON_API_IMAGE=%s\n' "$REFERENCE"
