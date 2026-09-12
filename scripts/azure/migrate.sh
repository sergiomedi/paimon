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

run_job "cj-paimon-migrate-${ENVIRONMENT}" migrate

case "$JOB_STATUS" in
    Succeeded)
        bold "▸ schema is up to date"
        printf '\n'
        printf 'The environment is complete. It bills for as long as it exists, so the next\n'
        printf 'two commands are the ones it was created for:\n'
        printf '  ./scripts/azure/token.sh      a token this deployment will accept\n'
        printf '  ./scripts/azure/measure.sh    the measured run, written to docs/measurements\n\n'
        ;;
    Failed)
        printf '\n'
        warn "Two causes account for most of these, and the logs above say which:"
        warn "  · the bootstrap has not run. A managed identity cannot authenticate until a"
        warn "    PostgreSQL role exists for it, and the migration authenticates as the"
        warn "    workload. Run ./scripts/azure/bootstrap.sh first."
        warn "  · a migration tried to CREATE EXTENSION. Only azure_pg_admin may create an"
        warn "    untrusted one, and Azure checks that before noticing it already exists, so"
        warn "    IF NOT EXISTS is no protection. The bootstrap creates extensions; a"
        warn "    migration has to ask the catalogue and skip."
        die "migration failed."
        ;;
    *)
        warn "Still ${JOB_STATUS} after five minutes. The job keeps running; check it with:"
        warn "  az containerapp job execution show --name cj-paimon-migrate-${ENVIRONMENT} \\"
        warn "    --resource-group $GROUP --job-execution-name $JOB_EXECUTION"
        ;;
esac
