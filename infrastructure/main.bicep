metadata description = '''
Paimon's Azure environment: everything the platform needs that is not the platform.

Deployed at subscription scope so the resource group is part of the template rather
than a prerequisite somebody has to remember. One environment is one resource group,
created and destroyed as a unit — see ADR-0035 for why that matters more here than
it would on a permanent deployment.
'''

targetScope = 'subscription'

@minLength(2)
@maxLength(10)
@description('Name of this environment. Appears in every resource name and tag, so keep it short and recognisable: dev, ci, demo.')
param environmentName string

@description('Region to deploy into. Model availability varies by region more than anything else does — check before changing it.')
param location string

@description('''
Object id of the person operating this environment, from `az ad signed-in-user show --query id -o tsv`.
Granted the data-plane roles a human needs and a workload does not: reading and writing
secrets. Empty means nobody is granted them, which is right for an unattended deployment
and wrong for one you intend to use from a laptop.
''')
param operatorPrincipalId string = ''

@description('Extra tags to merge onto every resource.')
param tags object = {}

@description('Days a deleted key vault stays recoverable. Seven is the minimum Azure allows, and the right choice for an environment that is created and destroyed repeatedly.')
@minValue(7)
@maxValue(90)
param keyVaultSoftDeleteDays int = 7

@description('Days of log retention in the workspace. Thirty is included in the per-GB price; beyond it is billed.')
@minValue(30)
@maxValue(730)
param logRetentionDays int = 30

@description('''
Model behind the chat deployment.

**Not every model in the catalogue is deployable here**, and the constraint is
this platform's rather than Azure's: the adapter sends `temperature` and
`max_tokens`, and the reasoning families accept neither — they want
`max_completion_tokens` and only their default temperature. A benchmark that
needs temperature zero to be reproducible cannot use a model that will not take
it. `az cognitiveservices model list --location <region>` says what exists; this
says what works.
''')
param chatModel string = 'gpt-4.1-mini'

@description('Version of the chat model. Region-specific, and not inferable from the name.')
param chatModelVersion string = '2025-04-14'

@description('''
Model behind the embedding deployment.

text-embedding-3-large natively produces 3072 dimensions and this platform's
index is built at 1024. They are reconciled by the adapter asking for 1024 in the
request, which the model supports, rather than by re-indexing at a width pgvector
cannot build an HNSW index over.
''')
param embeddingModel string = 'text-embedding-3-large'

@description('Version of the embedding model.')
param embeddingModelVersion string = '1'

@description('Thousands of tokens per minute for each deployment. The first thing to lower when a deployment fails on quota.')
@minValue(1)
param modelCapacity int = 10

@description('Azure AI Search tier. Free is 50 MB and three indexes: enough for the sample corpus, not for the benchmark.')
@allowed(['free', 'basic', 'standard'])
param searchSku string = 'basic'

// Deterministic across redeployments of the same environment in the same
// subscription, which is what makes `deploy` idempotent — and is also why
// `destroy` has to purge soft-deleted resources rather than leave their names
// held. See scripts/azure/destroy.sh.
var resourceToken = uniqueString(subscription().id, environmentName)

var allTags = union(tags, {
  application: 'paimon'
  environment: environmentName
  // Read by destroy.sh, and a standing instruction to anybody who finds this
  // resource group wondering whether it is safe to delete. It is.
  lifecycle: 'ephemeral'
  managedBy: 'bicep'
})

resource group 'Microsoft.Resources/resourceGroups@2021-04-01' = {
  name: 'rg-paimon-${environmentName}'
  location: location
  tags: allTags
}

module platform 'modules/platform.bicep' = {
  scope: group
  name: 'platform'
  params: {
    location: location
    environmentName: environmentName
    resourceToken: resourceToken
    operatorPrincipalId: operatorPrincipalId
    keyVaultSoftDeleteDays: keyVaultSoftDeleteDays
    logRetentionDays: logRetentionDays
    tags: allTags
  }
}

module ai 'modules/ai.bicep' = {
  scope: group
  name: 'ai'
  params: {
    location: location
    resourceToken: resourceToken
    identityPrincipalId: platform.outputs.identityPrincipalId
    operatorPrincipalId: operatorPrincipalId
    chatModel: chatModel
    chatModelVersion: chatModelVersion
    embeddingModel: embeddingModel
    embeddingModelVersion: embeddingModelVersion
    modelCapacity: modelCapacity
    searchSku: searchSku
    tags: allTags
  }
}

@description('Resource group holding this environment. Deleting it removes everything except the soft-deleted key vault.')
output resourceGroupName string = group.name

@description('Region everything was deployed into.')
output location string = location

@description('Name of the user-assigned identity every workload runs as.')
output identityName string = platform.outputs.identityName

@description('Client id of that identity, for DefaultAzureCredential when more than one identity is visible.')
output identityClientId string = platform.outputs.identityClientId

@description('Principal id of that identity, for role assignments made outside this template.')
output identityPrincipalId string = platform.outputs.identityPrincipalId

@description('Login server of the container registry, for docker push.')
output containerRegistryLoginServer string = platform.outputs.containerRegistryLoginServer

@description('Name of the container registry.')
output containerRegistryName string = platform.outputs.containerRegistryName

@description('Name of the Container Apps environment the API will run in.')
output containerAppsEnvironmentName string = platform.outputs.containerAppsEnvironmentName

@description('Default domain of that environment. An app named x is reachable at https://x.<domain>.')
output containerAppsDefaultDomain string = platform.outputs.containerAppsDefaultDomain

@description('URI of the key vault. Never a secret itself.')
output keyVaultUri string = platform.outputs.keyVaultUri

@description('Name of the key vault, which destroy.sh needs in order to purge it.')
output keyVaultName string = platform.outputs.keyVaultName

@description('Resource id of the Log Analytics workspace.')
output logAnalyticsWorkspaceId string = platform.outputs.logAnalyticsWorkspaceId

@description('Azure OpenAI endpoint, for PAIMON_AZURE_OPENAI__ENDPOINT.')
output openaiEndpoint string = ai.outputs.openaiEndpoint

@description('Name of the Azure OpenAI account, which destroy.sh needs in order to purge it.')
output openaiName string = ai.outputs.openaiName

@description('Chat deployment name, for PAIMON_AZURE_OPENAI__CHAT_DEPLOYMENT.')
output chatDeploymentName string = ai.outputs.chatDeploymentName

@description('Embedding deployment name, for PAIMON_AZURE_OPENAI__EMBEDDING_DEPLOYMENT.')
output embeddingDeploymentName string = ai.outputs.embeddingDeploymentName

@description('Azure AI Search endpoint, for PAIMON_AZURE_SEARCH__ENDPOINT.')
output searchEndpoint string = ai.outputs.searchEndpoint

@description('Name of the search service.')
output searchName string = ai.outputs.searchName
