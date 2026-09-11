metadata description = '''
The resources every later one depends on: an identity to be, somewhere to log,
somewhere to keep the few secrets that cannot be eliminated, somewhere to keep
images, and an environment to run them in.

Nothing here stores data and nothing here runs continuously, which is deliberate:
this deploys for cents, so the deployment path itself can be exercised without
spending the budget on it. The expensive resources arrive later and leave sooner.
'''

@description('Region to deploy into.')
param location string

@description('Name of this environment.')
param environmentName string

@description('Deterministic suffix that makes globally-scoped names unique to this subscription and environment.')
param resourceToken string

@description('Object id of the human operator, or empty for none.')
param operatorPrincipalId string

@description('Days a deleted key vault stays recoverable.')
param keyVaultSoftDeleteDays int

@description('Days of log retention in the workspace.')
param logRetentionDays int

@description('Tags applied to every resource.')
param tags object

// Built-in role definition ids. Written as constants with their names beside them
// because a bare GUID in a role assignment is unreviewable, and the whole point of
// this file is that somebody can read what it grants.
var roles = {
  // Pull images. Not push: the identity that runs the app has no reason to write
  // to the registry, and the one that builds it is CI's, not this one.
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  // Read secret values. The workload's role.
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
  // Read and write secret values. A person's role, never a workload's.
  keyVaultSecretsOfficer: 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
}

// ---------------------------------------------------------------------------
// Identity
// ---------------------------------------------------------------------------

// User-assigned rather than system-assigned, for two reasons that both bite in
// practice. It outlives the app, so tearing the API down and putting it back does
// not invalidate every role assignment pointing at it. And it exists *before* the
// app does, which breaks the ordering deadlock of needing AcrPull granted to an
// identity that only exists once the app that pulls the image has started.
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-paimon-${environmentName}'
  location: location
  tags: tags
}

// ---------------------------------------------------------------------------
// Logs
// ---------------------------------------------------------------------------

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-paimon-${environmentName}'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: logRetentionDays
    features: {
      // Without this, every principal with reader on the workspace can read every
      // log line in it. The workspace is not the place to discover that logs are
      // a data-plane concern.
      enableLogAccessUsingOnlyResourcePermissions: true
    }
  }
}

// ---------------------------------------------------------------------------
// Secrets
// ---------------------------------------------------------------------------

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'kv-paimon-${resourceToken}'
  location: location
  tags: tags
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    // RBAC, not access policies. Access policies are a second, parallel
    // authorization system that Azure RBAC cannot see, which means a review of
    // "who can read this" has to be done twice and is therefore done once.
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: keyVaultSoftDeleteDays
    // Purge protection is deliberately NOT enabled. It cannot be turned off once
    // on, and it would make the name of this vault unusable for the length of the
    // retention window after every teardown — which for an environment that is
    // destroyed on purpose turns a feature into a trap. A permanent environment
    // should enable it; this is not one.
    publicNetworkAccess: 'Enabled'
  }
}

// ---------------------------------------------------------------------------
// Images
// ---------------------------------------------------------------------------

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'crpaimon${resourceToken}'
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    // The admin account is a username and password that every pull would share
    // and nobody would rotate. There is an identity three resources above this
    // one that exists precisely so that it is not needed.
    adminUserEnabled: false
  }
}

// ---------------------------------------------------------------------------
// Somewhere to run
// ---------------------------------------------------------------------------

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: 'cae-paimon-${environmentName}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: workspace.properties.customerId
        sharedKey: workspace.listKeys().primarySharedKey
      }
    }
    // A workload-profiles environment even though every workload here runs on the
    // Consumption profile. The alternative, a Consumption-only environment, is the
    // legacy shape: it cannot take a custom VNet subnet with user-defined routes,
    // cannot take private endpoints, and cannot be converted afterwards. The two
    // cost the same while nothing is running, so choosing the one that can grow is
    // free, and choosing the one that cannot is a migration later.
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    zoneRedundant: false
  }
}

// ---------------------------------------------------------------------------
// Who may do what
// ---------------------------------------------------------------------------

// Deterministic names, so a redeployment updates the assignment rather than
// failing on a duplicate or creating a second one.
resource identityPullsImages 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, identity.id, roles.acrPull)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource identityReadsSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, identity.id, roles.keyVaultSecretsUser)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsUser)
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// The operator gets a strictly larger role than the workload, on purpose: somebody
// has to be able to put a secret in, and it should not be the thing that reads it.
resource operatorWritesSecrets 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (!empty(operatorPrincipalId)) {
  scope: vault
  name: guid(vault.id, operatorPrincipalId, roles.keyVaultSecretsOfficer)
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsOfficer)
    principalId: operatorPrincipalId
    // Left unset rather than guessed: 'User' is wrong when a pipeline's service
    // principal is passed here, and Azure infers it correctly from the object id.
  }
}

output identityName string = identity.name
output identityClientId string = identity.properties.clientId
output identityPrincipalId string = identity.properties.principalId
output containerRegistryName string = registry.name
output containerRegistryLoginServer string = registry.properties.loginServer
output containerAppsEnvironmentName string = environment.name
output containerAppsDefaultDomain string = environment.properties.defaultDomain
output keyVaultName string = vault.name
output keyVaultUri string = vault.properties.vaultUri
output logAnalyticsWorkspaceId string = workspace.id
