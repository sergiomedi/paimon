metadata description = '''
The database, and the network that makes it unreachable from anywhere else.

This is the only resource in the environment that holds data, and it is the only
one that is not on the internet. Public network access is off; the single way in
is a private endpoint on the subnet the application runs in.

It also has **no password**. Not a password in a vault, not a password in an
environment variable — the server is created with password authentication
disabled outright, so the only way to authenticate is a Microsoft Entra token.
That is the same argument as ADR-0037, applied to the one place where being
wrong would cost data rather than tokens.
'''

@description('Region to deploy into.')
param location string

@description('Name of this environment.')
param environmentName string

@description('Deterministic suffix for globally-scoped names.')
param resourceToken string

@description('Resource id of the subnet private endpoints live in.')
param privateEndpointSubnetId string

@description('Resource id of the virtual network, for the private DNS zone link.')
param virtualNetworkId string

@description('''
Object id of a person to register as a Microsoft Entra administrator.

Only used when registerOperatorAsAdministrator is on. The administrator that does
the work is the management identity below; this one is for a human who wants to
connect from inside the network with their own account.
''')
param administratorPrincipalId string

@description('Sign-in name of that person, which Azure stores alongside the object id and which becomes a PostgreSQL role name.')
param administratorPrincipalName string

@description('''
Whether to register the person running the deployment as a database administrator.

**Off**, and the reason is a hard limit rather than a preference. A PostgreSQL
role name cannot exceed 63 characters, and Azure registers an Entra administrator
under its sign-in name — so a guest account's UPN, which looks like
`someone_gmail.com#EXT#@tenantname.onmicrosoft.com` and runs to 67 characters
here, is silently truncated to 63 when the role is created and then fails
`42710: role already exists` on the next deployment, because the name Azure asks
for and the name PostgreSQL stored are no longer the same string. There is no
version of that which eventually works.

It is also unnecessary. Since ADR-0042 the administrator that matters is the
management identity, whose name is short and generated; and since ADR-0038 the
server has no public address, so a person's account cannot reach it from a laptop
whether or not it is registered. This was a decorative administrator that did not
fit.

Turn it on for a sign-in name comfortably under 63 characters, on a deployment
where somebody works from inside the network.
''')
param registerOperatorAsAdministrator bool = false

@description('Principal id of the administration identity, registered as a second Entra administrator so that the bootstrap job can create the workload role without a person at a terminal.')
param administrationIdentityPrincipalId string

@description('Name of that identity, which Azure stores beside its object id and which is also its PostgreSQL role name.')
param administrationIdentityName string

@description('Compute tier. Burstable is explicitly not recommended for production and supports no high availability; vector search is exactly the workload that exhausts its CPU credits.')
param databaseSku string

@description('Storage in gibibytes. Thirty-two is the floor Azure allows, and storage only ever scales up.')
@minValue(32)
param databaseStorageGb int

@description('PostgreSQL major version.')
param postgresVersion string

@description('Tags applied to every resource.')
param tags object

var serverName = 'psql-paimon-${resourceToken}'
var databaseName = 'paimon'

// The zone name is fixed by Azure: a private endpoint for PostgreSQL resolves
// through this exact name and no other. Getting it wrong produces a private
// endpoint that exists and a hostname that still resolves to the public address.
var dnsZoneName = 'privatelink.postgres.database.azure.com'

resource server 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: serverName
  location: location
  tags: tags
  sku: {
    name: databaseSku
    tier: 'GeneralPurpose'
  }
  properties: {
    version: postgresVersion
    // No administratorLogin and no administratorLoginPassword. Their absence is
    // the feature: there is no credential to rotate, leak or commit.
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Disabled'
      tenantId: subscription().tenantId
    }
    storage: {
      storageSizeGB: databaseStorageGb
      autoGrow: 'Disabled'
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    network: {
      // Not "public with an empty firewall". Off. The private endpoint below is
      // the only route, and an accidental firewall rule cannot open one.
      publicNetworkAccess: 'Disabled'
    }
  }
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: server
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

// Without this there is no way in at all: Entra-only authentication with no Entra
// administrator is a server nobody can connect to, including to fix it.


// Without this, `CREATE EXTENSION vector` fails on a server that is otherwise
// perfectly configured — Azure refuses to create an extension that is not on the
// server's allow-list, whoever asks. The local pgvector image needs no
// equivalent, which is exactly why this is the kind of difference that is found
// in a deployment rather than in a test.
resource extensions 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: server
  name: 'azure.extensions'
  properties: {
    value: 'VECTOR'
    source: 'user-override'
  }
}

// ---------------------------------------------------------------------------
// The only route in
// ---------------------------------------------------------------------------

resource dnsZone 'Microsoft.Network/privateDnsZones@2024-06-01' = {
  name: dnsZoneName
  location: 'global'
  tags: tags
}

resource dnsLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2024-06-01' = {
  parent: dnsZone
  name: 'link-${environmentName}'
  location: 'global'
  tags: tags
  properties: {
    virtualNetwork: {
      id: virtualNetworkId
    }
    // No registration: this zone exists to resolve one name that Azure manages,
    // not to collect records from every machine on the network.
    registrationEnabled: false
  }
}

resource privateEndpoint 'Microsoft.Network/privateEndpoints@2024-05-01' = {
  name: 'pep-psql-${environmentName}'
  location: location
  tags: tags
  properties: {
    subnet: {
      id: privateEndpointSubnetId
    }
    privateLinkServiceConnections: [
      {
        name: 'psql'
        properties: {
          privateLinkServiceId: server.id
          groupIds: ['postgresqlServer']
        }
      }
    ]
  }
}

// The endpoint without this group is an endpoint nothing uses: it creates the
// private address but nothing writes the A record, so the server's hostname keeps
// resolving to a public address that now refuses connections.
resource dnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2024-05-01' = {
  parent: privateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'postgres'
        properties: {
          privateDnsZoneId: dnsZone.id
        }
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// The administrators, last
// ---------------------------------------------------------------------------

// Deliberately after the private endpoint and its DNS zone group, rather than
// beside the server where they read more naturally.
//
// `AadAuthOperationCannotBePerformedWhenServerIsNotAccessible` is a race Azure
// has had open since 2023 and has not fixed: a flexible server reports
// `Succeeded` before it is ready for Microsoft Entra principal operations, and
// `dependsOn` is satisfied by that `Succeeded`. The first real deployment of this
// template hit it on the first administrator.
//
// This template made it worse by creating the private endpoint *in parallel*
// with the administrators, so at the moment of the call a server with no public
// address also had no private one. Ordering them behind the route is both the
// honest dependency — there is no sense in which an administrator can be added to
// a server nothing can reach — and several minutes of settling time, which is
// what actually makes the race unlikely.
//
// It does not make it impossible. The mitigation for the remainder is that this
// deployment is idempotent: running it again completes, and deploy.sh says so
// when it recognises the error.
resource administrator 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2024-08-01' =
  if (registerOperatorAsAdministrator && !empty(administratorPrincipalId)) {
    parent: server
    name: administratorPrincipalId
    properties: {
      principalType: 'User'
      principalName: administratorPrincipalName
      tenantId: subscription().tenantId
    }
    dependsOn: [
      dnsZoneGroup
    ]
  }

// The identity the bootstrap job runs as (ADR-0042). principalType is
// 'ServicePrincipal' rather than 'User': a managed identity is one, and getting
// this wrong produces a server that accepts the registration and then refuses the
// sign-in.
resource administrationPrincipal 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2024-08-01' = {
  parent: server
  name: administrationIdentityPrincipalId
  properties: {
    principalType: 'ServicePrincipal'
    principalName: administrationIdentityName
    tenantId: subscription().tenantId
  }
  dependsOn: [
    // The route, for the reason above. Not the other administrator, which may
    // not exist: Azure applies these one at a time and rejects a second while the
    // first is still being applied, so when both are on the conditional one is
    // ordered first by being the one that waits on the endpoint.
    dnsZoneGroup
    administrator
  ]
}

@description('Hostname to connect to. Resolves to a private address from inside the virtual network and to nothing useful from outside it.')
output databaseHost string = server.properties.fullyQualifiedDomainName

output databaseName string = database.name
output databaseServerName string = server.name