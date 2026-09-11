#!/usr/bin/env bash
# Create the workload's PostgreSQL role and the extensions the schema needs.
#
#   ./scripts/azure/bootstrap.sh
#
# Run once per environment, before migrate.sh. It is idempotent: running it again
# reports what already exists and changes nothing.
#
# This is SQL, not ARM, which is why it is a job rather than a few more lines of
# Bicep. A Microsoft Entra principal cannot connect to PostgreSQL until a role
# exists for it inside the database, and creating one is an administrator's
# privilege. Since ADR-0038 the database has no public address either, so there
# is nowhere outside the network to run it from — see ADR-0042.

source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

require_az
announce

run_job "cj-paimon-bootstrap-${ENVIRONMENT}" bootstrap

case "$JOB_STATUS" in
    Succeeded)
        bold "▸ the database is ready for the schema"
        printf 'Next: ./scripts/azure/migrate.sh\n'
        ;;
    Failed)
        printf '\n'
        warn "Two causes account for most of these, and the logs above say which:"
        warn "  · the administration identity is not yet an Entra administrator of the"
        warn "    server, or its role assignment has not propagated. Wait and retry."
        warn "  · the vector extension is not allow-listed on the server. That is the"
        warn "    azure.extensions configuration in infrastructure/modules/data.bicep."
        die "bootstrap failed."
        ;;
    *)
        warn "Still ${JOB_STATUS} after five minutes. Check it with:"
        warn "  az containerapp job execution show --name cj-paimon-bootstrap-${ENVIRONMENT} \\"
        warn "    --resource-group $GROUP --job-execution-name $JOB_EXECUTION"
        ;;
esac
