#!/usr/bin/env bash
# Put a new version in front of traffic, having checked it first.
#
#   ./scripts/azure/release.sh              release the newest published image
#   ./scripts/azure/release.sh <image>      release a particular one
#
# Deploys the image as a revision that takes **no traffic**, gives it a hostname
# of its own, verifies it through that hostname, and only then shifts weight to
# it. The revision that was serving stays running and keeps its label, so undoing
# this is one command and a few seconds — ./scripts/azure/rollback.sh.
#
# ── Why a release is not a deployment ────────────────────────────────────────
#
# Through Phase 7 these were the same act: deploy.sh replaced what was running,
# and the only way back was another deployment — a rebuild, minutes, and no way
# to look at the new version before it served every request. That is fine for an
# environment built to be destroyed and unacceptable for one somebody uses.
#
# What makes the difference is multiple active revisions (ADR-0044). A revision
# can exist, run, be reachable and hold no traffic; traffic is a weight, and a
# weight can be changed in either direction in seconds.

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

require_az
announce
load_outputs

APP="${AZURE_API_NAME:-ca-paimon-api-${ENVIRONMENT}}"
IMAGE="${1:-$(api_image)}"
[[ -n "$IMAGE" ]] || die "$(printf '%s\n' \
    "no image to release: this registry has no tags." \
    "" \
    "  ./scripts/azure/publish.sh")"

# The one about to be replaced, read from the platform rather than remembered.
BLUE="$(live_revision)"
GREEN="${IMAGE##*:}"

bold "▸ what this would do"
printf '  from   %s\n' "${BLUE:-nothing is serving yet}"
printf '  to     %s\n' "$GREEN"
printf '  image  %s\n\n' "$IMAGE"

if [[ "$BLUE" == "$GREEN" ]]; then
    warn "That revision is already serving. Nothing to release."
    exit 0
fi

# ── The new revision, with no traffic ────────────────────────────────────────
#
# Deployed by the same template as everything else, with the traffic parameter
# still naming the current revision. That is the whole trick: ARM creates the
# revision and does not give it anything.
bold "▸ deploying ${GREEN}, with no traffic"
AZURE_PAIMON_API_IMAGE="$IMAGE" \
    AZURE_PAIMON_API_TRAFFIC_REVISION="$BLUE" \
    "$SCRIPTS/deploy.sh" --yes

# ── Labels ───────────────────────────────────────────────────────────────────
#
# A label is a stable name for a moving target, and the reason it exists is the
# hostname it brings: a revision with a label can be called directly, without
# taking a single request away from what is serving.
bold "▸ labelling"
if [[ -n "$BLUE" ]]; then
    az containerapp revision label add --name "$APP" --resource-group "$GROUP" \
        --label blue --revision "${APP}--${BLUE}" -o none 2>/dev/null || true
fi
az containerapp revision label add --name "$APP" --resource-group "$GROUP" \
    --label green --revision "${APP}--${GREEN}" -o none
printf '  green -> %s\n\n' "$GREEN"

# ── The check that makes this worth doing ────────────────────────────────────
GREEN_URL="$(labelled_url green)" || die "no default domain in the outputs; deploy first."
bold "▸ checking ${GREEN_URL}"

# Readiness rather than liveness: it opens the database, the cache and the model
# endpoint, so it is the cheapest request that proves this revision can actually
# serve rather than merely start. A revision that starts and cannot reach its
# database is exactly the release this step exists to stop.
READY=""
for _ in $(seq 1 30); do
    READY="$(curl --silent --show-error --max-time 30 --output /dev/null \
        --write-out '%{http_code}' "${GREEN_URL}/api/v1/health/ready" || printf '000')"
    [[ "$READY" == "200" ]] && break
    printf '.'
    sleep 10
done
printf '\n'

if [[ "$READY" != "200" ]]; then
    warn "  ${READY} — the new revision is not ready, so it is not getting traffic."
    warn ""
    warn "  It is deployed and idle, which costs nothing while it serves nothing."
    warn "  ${BLUE:-the previous revision} is still serving every request."
    warn ""
    warn "  az containerapp logs show --name $APP --resource-group $GROUP \\"
    warn "    --revision ${APP}--${GREEN} --tail 100"
    die "nothing was released."
fi
printf '  200 — ready\n\n'

# ── The release itself ───────────────────────────────────────────────────────
bold "▸ shifting traffic to ${GREEN}"
az containerapp ingress traffic set --name "$APP" --resource-group "$GROUP" \
    --revision-weight "${APP}--${GREEN}=100" -o none
printf '  done\n\n'

bold "▸ released"
printf 'Serving   %s\n' "$GREEN"
printf 'Standing by %s\n\n' "${BLUE:-nothing}"
if [[ -n "$BLUE" ]]; then
    printf 'To undo it — seconds, no rebuild, the revision is still running:\n'
    printf '  ./scripts/azure/rollback.sh\n\n'
fi
printf 'A deployment from here on keeps this revision serving: deploy.sh reads what is\n'
printf 'live and passes it back to the template, so an unrelated change cannot move it.\n'
