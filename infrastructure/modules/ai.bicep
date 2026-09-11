metadata description = '''
The two managed services this platform has adapters for and has never actually
talked to: Azure OpenAI, and Azure AI Search.

Both are created with **local authentication disabled**. Not "a key exists and we
choose not to use it" — the key path is switched off at the resource, so a
regression that reaches for one fails loudly instead of quietly working. That is
the only way a claim like "this platform stores no credentials" can be checked
rather than asserted.
'''

@description('Region to deploy into.')
param location string

@description('Deterministic suffix for globally-scoped names.')
param resourceToken string

@description('Principal id of the workload identity.')
param identityPrincipalId string

@description('Object id of the human operator, or empty for none.')
param operatorPrincipalId string

@description('Model backing the chat deployment. Must accept `temperature` and `max_tokens` — see the README in this directory.')
param chatModel string

@description('Version of the chat model. Availability is per region; `az cognitiveservices model list` is the authority.')
param chatModelVersion string

@description('Model backing the embedding deployment.')
param embeddingModel string

@description('Version of the embedding model.')
param embeddingModelVersion string

@description('Thousands of tokens per minute for each deployment. Lower this first when a deployment fails on quota.')
@minValue(1)
param modelCapacity int

@description('Azure AI Search tier. Free is 50 MB and three indexes, which is smaller than the benchmark corpus.')
@allowed(['free', 'basic', 'standard'])
param searchSku string

@description('Tags applied to every resource.')
param tags object

var roles = {
  // Call a deployment for inference. Not read keys, not create deployments, not
  // touch quota — the smallest role that lets this platform work.
  cognitiveServicesOpenAIUser: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
  // Read and write documents in an index, and query it. The workload's role.
  searchIndexDataContributor: '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
  // Create and change index *definitions*. Notably cannot query or load
  // documents, and can retrieve admin keys — a deploy-time role, never a
  // runtime one.
  searchServiceContributor: '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
}

// ---------------------------------------------------------------------------
// Models
// ---------------------------------------------------------------------------

resource openai 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: 'oai-paimon-${resourceToken}'
  location: location
  tags: tags
  kind: 'OpenAI'
  sku: {
    name: 'S0'
  }
  properties: {
    // Required, and the requirement is not obvious: without a custom subdomain
    // the account is reachable only at the regional endpoint, which does not
    // accept Microsoft Entra tokens. A keyless deployment that skips this fails
    // at the first call with a 401 that says nothing about subdomains.
    customSubDomainName: 'oai-paimon-${resourceToken}'
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true
  }
}

// Deployments are created one at a time. Azure rejects concurrent deployment
// operations on a single account, and without the dependency below Bicep is free
// to submit both at once — which fails intermittently, on about half of runs,
// which is the worst kind of failure to debug.
resource embeddingDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: openai
  name: 'paimon-embed'
  sku: {
    name: 'GlobalStandard'
    capacity: modelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: embeddingModel
      version: embeddingModelVersion
    }
  }
}

resource chatDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: openai
  name: 'paimon-chat'
  dependsOn: [embeddingDeployment]
  sku: {
    name: 'GlobalStandard'
    capacity: modelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: chatModel
      version: chatModelVersion
    }
  }
}

// ---------------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------------

resource search 'Microsoft.Search/searchServices@2023-11-01' = {
  name: 'srch-paimon-${resourceToken}'
  location: location
  tags: tags
  sku: {
    name: searchSku
  }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'default'
    publicNetworkAccess: 'enabled'
    // A new search service is key-only until this is changed, and role
    // assignments alone do not make it answer: every keyless request is refused
    // until the service itself accepts them. Disabling local auth switches it to
    // role-based entirely, which is both the posture we want and the setting
    // most guides forget to mention beside the role assignments.
    disableLocalAuth: true
  }
}

// ---------------------------------------------------------------------------
// Who may do what
// ---------------------------------------------------------------------------

resource identityCallsModels 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: openai
  name: guid(openai.id, identityPrincipalId, roles.cognitiveServicesOpenAIUser)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      roles.cognitiveServicesOpenAIUser
    )
    principalId: identityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource identityWritesDocuments 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: search
  name: guid(search.id, identityPrincipalId, roles.searchIndexDataContributor)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      roles.searchIndexDataContributor
    )
    principalId: identityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// The operator gets the same two, because the first thing this platform does
// against these services is run its benchmark from a laptop.
resource operatorCallsModels 'Microsoft.Authorization/roleAssignments@2022-04-01' =
  if (!empty(operatorPrincipalId)) {
    scope: openai
    name: guid(openai.id, operatorPrincipalId, roles.cognitiveServicesOpenAIUser)
    properties: {
      roleDefinitionId: subscriptionResourceId(
        'Microsoft.Authorization/roleDefinitions',
        roles.cognitiveServicesOpenAIUser
      )
      principalId: operatorPrincipalId
    }
  }

resource operatorWritesDocuments 'Microsoft.Authorization/roleAssignments@2022-04-01' =
  if (!empty(operatorPrincipalId)) {
    scope: search
    name: guid(search.id, operatorPrincipalId, roles.searchIndexDataContributor)
    properties: {
      roleDefinitionId: subscriptionResourceId(
        'Microsoft.Authorization/roleDefinitions',
        roles.searchIndexDataContributor
      )
      principalId: operatorPrincipalId
    }
  }

// And one the workload does not get: creating and changing index *definitions*.
// The index schema is the application's, created by a deliberate command rather
// than by whatever happens to start first.
resource operatorManagesIndexes 'Microsoft.Authorization/roleAssignments@2022-04-01' =
  if (!empty(operatorPrincipalId)) {
    scope: search
    name: guid(search.id, operatorPrincipalId, roles.searchServiceContributor)
    properties: {
      roleDefinitionId: subscriptionResourceId(
        'Microsoft.Authorization/roleDefinitions',
        roles.searchServiceContributor
      )
      principalId: operatorPrincipalId
    }
  }

output openaiEndpoint string = openai.properties.endpoint
output openaiName string = openai.name
output chatDeploymentName string = chatDeployment.name
output embeddingDeploymentName string = embeddingDeployment.name
output searchEndpoint string = 'https://${search.name}.search.windows.net'
output searchName string = search.name
