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
param location = readEnvironmentVariable('PAIMON_AZURE_LOCATION', 'swedencentral')

// az ad signed-in-user show --query id -o tsv
param operatorPrincipalId = readEnvironmentVariable('PAIMON_AZURE_OPERATOR_ID', '')

// az ad signed-in-user show --query userPrincipalName -o tsv
//
// Azure stores this beside the object id of the database administrator and
// rejects a create where the two disagree, which is a confusing failure: the
// object id is right, the principal exists, and the message is about a name.
param administratorPrincipalName = readEnvironmentVariable('PAIMON_AZURE_OPERATOR_NAME', '')

// The image to run, registry included. Set by scripts/azure/deploy.sh, which
// resolves it from the registry when PAIMON_API_IMAGE is not set and refuses to
// deploy when the registry is empty. There is deliberately no usable default: a
// placeholder here would produce a Container App that exists, passes no probe,
// and has to be noticed rather than announced.
param apiImage = readEnvironmentVariable('PAIMON_API_IMAGE', 'none')

// False only on the first deployment of a new environment, when the registry
// this template creates is necessarily still empty. deploy.sh works it out by
// asking the registry and sets it; nobody sets it by hand.
param deployApi = readEnvironmentVariable('PAIMON_DEPLOY_API', 'true') == 'true'

// Application id URI of the app registration the API validates tokens for, for
// example api://paimon. See "Before you start" in docs/deployment.md — this is
// the one thing the template cannot create, because an Entra app registration is
// not an ARM resource.
param apiAudience = readEnvironmentVariable('PAIMON_AZURE_API_AUDIENCE', 'api://paimon')

// The tenant whose tokens are accepted. Empty means the tenant being deployed
// into, which is the right answer whenever the API and its callers live together.
param apiTenantId = readEnvironmentVariable('PAIMON_AZURE_TENANT_ID', '')

// Price list for cost attribution, as JSON:
//   {"gpt-4.1-mini": {"input": 0.4, "output": 1.6}}
//
// Empty by default and deliberately not filled in here. Cost is token counts
// times a table somebody typed — the invoice is the authority — so the numbers
// belong to whoever read the pricing page that day, not to a default committed
// months earlier. A model absent from the table produces no cost measurement
// rather than a cost of zero, because zero is a claim.
param modelPrices = json(readEnvironmentVariable('PAIMON_AZURE_MODEL_PRICES', '{}'))

// A label for that table, recorded on every measurement so a figure can be
// traced back to the prices that produced it. Required as soon as there are any.
param priceRevision = readEnvironmentVariable('PAIMON_AZURE_PRICE_REVISION', 'unset')
