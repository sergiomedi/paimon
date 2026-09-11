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

// Sweden Central: Azure OpenAI model availability is the binding constraint on
// this choice, and it has been consistently early there. Change it only after
// checking the model availability table, not on latency intuition.
param location = readEnvironmentVariable('PAIMON_AZURE_LOCATION', 'swedencentral')

// az ad signed-in-user show --query id -o tsv
param operatorPrincipalId = readEnvironmentVariable('PAIMON_AZURE_OPERATOR_ID', '')
