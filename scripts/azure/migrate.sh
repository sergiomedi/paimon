#!/usr/bin/env bash
# Bring the database schema up to date.
#
#   ./scripts/azure/migrate.sh
#
# Starts the migration job and waits for it, then prints its logs. The job runs
# the deployed image with `alembic upgrade head`.
#
# This is not a convenience wrapper around something you could do yourself. Since
# ADR-0038 the database has no public address, so there is no route to it from a
# laptop at all: the migration has to run from inside the virtual network, and a
# Container Apps job is the thing already there. See ADR-0040.

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

require_az
announce

JOB="cj-paimon-migrate-${ENVIRONMENT}"
az containerapp job show --name "$JOB" --resource-group "$GROUP" -o none 2>/dev/null \
    || die "no migration job in ${GROUP}. Deploy the environment first: ./scripts/azure/deploy.sh"

bold "▸ starting ${JOB}"
EXECUTION="$(az containerapp job start --name "$JOB" --resource-group "$GROUP" \
    --query name -o tsv)"
printf 'execution    %s\n\n' "$EXECUTION"

bold "▸ waiting"
# No --wait on `job start`, so this polls. Thirty tries at ten seconds covers the
# five minutes a cold image pull and a schema change take between them; the job's
# own replicaTimeout is half an hour and is the real limit.
STATUS="Running"
for _ in $(seq 1 30); do
    STATUS="$(az containerapp job execution show --name "$JOB" --resource-group "$GROUP" \
        --job-execution-name "$EXECUTION" --query properties.status -o tsv 2>/dev/null || echo Unknown)"
    [[ "$STATUS" == "Running" || "$STATUS" == "Unknown" ]] || break
    printf '.'
    sleep 10
done
printf '\n\n'

bold "▸ logs"
# Log ingestion lags the execution by a few seconds, which is long enough that a
# job that has just finished often has nothing to show yet.
sleep 5
az containerapp job logs show --name "$JOB" --resource-group "$GROUP" \
    --container migrate --execution "$EXECUTION" --tail 100 2>/dev/null \
    || warn "  no logs yet. Try again in a moment, or look in Log Analytics."

printf '\n'
case "$STATUS" in
    Succeeded)
        bold "▸ schema is up to date"
        ;;
    Failed)
        printf '\n'
        warn "The usual first cause is the manual bootstrap: a managed identity cannot"
        warn "authenticate until a PostgreSQL role exists for it. See \"The one manual step\""
        warn "in docs/deployment.md."
        die "migration failed."
        ;;
    *)
        warn "Still ${STATUS} after five minutes. The job keeps running; check it with:"
        warn "  az containerapp job execution show --name $JOB --resource-group $GROUP \\"
        warn "    --job-execution-name $EXECUTION"
        ;;
esac
