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

@description('Address space of the virtual network everything runs in.')
param virtualNetworkPrefix string

@description('Subnet the Container Apps environment is injected into. Workload profiles need at least a /27; a /23 leaves room to grow without renumbering.')
param appsSubnetPrefix string

@description('Subnet private endpoints live in. Nothing runs here; it holds network interfaces.')
param privateEndpointSubnetPrefix string

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

// A second identity, which exists to hold one privilege the first must not have.
//
// The database has no password and no public address, so the workload's
// PostgreSQL role has to be created from inside the network by a Microsoft Entra
// administrator — and the thing a role is created *for* should not be the thing
// that creates it (ADR-0038, and ADR-0042 for what was done about it). This
// identity is a database administrator; the workload's is a plain user. Nothing
// serving traffic ever runs as this one: it is used by a single job that runs
// once per environment.
resource administrationIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-paimon-dbadmin-${environmentName}'
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
// The network
// ---------------------------------------------------------------------------

// A virtual network exists here for one reason: the database has no public
// address, and something has to be on the inside with it. The API keeps a public
// ingress — this is not an isolated deployment, it is a deployment whose data
// tier is unreachable from the internet, which is a different and more honest
// claim.
resource network 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: 'vnet-paimon-${environmentName}'
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [virtualNetworkPrefix]
    }
    subnets: [
      {
        name: 'snet-apps'
        properties: {
          addressPrefix: appsSubnetPrefix
          // Container Apps takes the subnet over: the delegation is what lets it
          // place infrastructure there, and the environment refuses a subnet
          // without it.
          delegations: [
            {
              name: 'container-apps'
              properties: {
                serviceName: 'Microsoft.App/environments'
              }
            }
          ]
        }
      }
      {
        name: 'snet-data'
        properties: {
          addressPrefix: privateEndpointSubnetPrefix
        }
      }
    ]
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
    // Fixed when the environment is created and not changeable afterwards, which
    // is the reason a Consumption-only environment was rejected in ADR-0034 and
    // the reason this is here from the start rather than added when the database
    // arrives: on a permanent deployment, adding it later is a rebuild.
    vnetConfiguration: {
      infrastructureSubnetId: network.properties.subnets[0].id
    }
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
// The resource id, not just the principal id: a container app names the identity
// it pulls images with by resource id, and the two are not interchangeable.
output identityResourceId string = identity.id
output identityClientId string = identity.properties.clientId
output identityPrincipalId string = identity.properties.principalId

@description('Name of the administration identity. It is also its PostgreSQL role name, because that is how a managed identity signs in.')
output administrationIdentityName string = administrationIdentity.name

@description('Resource id of the administration identity.')
output administrationIdentityResourceId string = administrationIdentity.id

@description('Client id of the administration identity, so a job can name which identity to authenticate as.')
output administrationIdentityClientId string = administrationIdentity.properties.clientId

@description('Principal id of the administration identity, for registering it as the database Entra administrator.')
output administrationIdentityPrincipalId string = administrationIdentity.properties.principalId
output containerRegistryName string = registry.name
output containerRegistryLoginServer string = registry.properties.loginServer
output containerAppsEnvironmentName string = environment.name
output containerAppsDefaultDomain string = environment.properties.defaultDomain
output keyVaultName string = vault.name
output keyVaultUri string = vault.properties.vaultUri
output logAnalyticsWorkspaceId string = workspace.id
output virtualNetworkId string = network.id
output privateEndpointSubnetId string = network.properties.subnets[1].id
