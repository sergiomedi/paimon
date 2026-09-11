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
Object id of the Microsoft Entra principal that administers the database.

Required, and it is a person rather than the workload: somebody has to be able to
create the workload's role, and the thing that role is for should not be able to
create it.
''')
param administratorPrincipalId string

@description('Display name of that principal, which Azure stores alongside the object id.')
param administratorPrincipalName string

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
resource administrator 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2024-08-01' = {
  parent: server
  name: administratorPrincipalId
  properties: {
    principalType: 'User'
    principalName: administratorPrincipalName
    tenantId: subscription().tenantId
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

@description('Hostname to connect to. Resolves to a private address from inside the virtual network and to nothing useful from outside it.')
output databaseHost string = server.properties.fullyQualifiedDomainName

output databaseName string = database.name
output databaseServerName string = server.name
