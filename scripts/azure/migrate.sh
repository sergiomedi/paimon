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
        ;;
    Failed)
        printf '\n'
        warn "The usual first cause is that the bootstrap has not run: a managed identity"
        warn "cannot authenticate until a PostgreSQL role exists for it, and the migration"
        warn "authenticates as the workload. Run ./scripts/azure/bootstrap.sh first."
        die "migration failed."
        ;;
    *)
        warn "Still ${JOB_STATUS} after five minutes. The job keeps running; check it with:"
        warn "  az containerapp job execution show --name cj-paimon-migrate-${ENVIRONMENT} \\"
        warn "    --resource-group $GROUP --job-execution-name $JOB_EXECUTION"
        ;;
esac
