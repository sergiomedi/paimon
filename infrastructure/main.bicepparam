// Parameters for an ephemeral environment.
//
// Read from the shell rather than written here, because two of the three are
// facts about whoever is deploying — their subscription, their object id — and a
// parameter file in git that carries those is a parameter file that is edited
// before every deployment and committed by accident after one of them.
//
// The defaults are the ones scripts/azure/deploy.sh uses when nothing is set.

using 'main.bicep'

param environmentName = readEnvironmentVariable('PAIMON_AZURE_ENV', 'dev')

// West Europe, and the reason is evidence rather than preference. Sweden Central
// was the first choice on model-availability grounds and turned out to have no
// GlobalStandard quota for the embedding model and no capacity for a Basic
// search service — both discovered by a failed deployment, both invisible to
// `what-if`. Before changing this, run `scripts/azure/preview.sh`: it validates
// against the region first, which is how those two are caught in seconds.
param location = readEnvironmentVariable('PAIMON_AZURE_LOCATION', 'westeurope')

// az ad signed-in-user show --query id -o tsv
param operatorPrincipalId = readEnvironmentVariable('PAIMON_AZURE_OPERATOR_ID', '')

// az ad signed-in-user show --query userPrincipalName -o tsv
//
// Azure stores this beside the object id of the database administrator and
// rejects a create where the two disagree, which is a confusing failure: the
// object id is right, the principal exists, and the message is about a name.
param administratorPrincipalName = readEnvironmentVariable('PAIMON_AZURE_OPERATOR_NAME', '')
