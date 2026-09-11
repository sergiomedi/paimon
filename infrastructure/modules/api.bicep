metadata description = '''
The application itself: one Container App serving the HTTP API and the MCP
endpoint, and one job that migrates the database.

Everything this app needs to reach — models, search, the database — it reaches
with the managed identity it runs as. There is not one secret in this file, and
the `secrets` array below is empty on purpose rather than by omission.

The job exists because of ADR-0038. A database with no public address cannot be
migrated from a laptop, so the migration has to run somewhere inside the network,
and the only thing already inside the network is this environment.
'''

@description('Region to deploy into.')
param location string

@description('Name of this environment.')
param environmentName string

@description('Name of the Container Apps environment to run in.')
param containerAppsEnvironmentName string

@description('Default domain of that environment. The app is reachable at https://<app>.<domain>.')
param containerAppsDefaultDomain string

@description('Login server of the registry the image is pulled from.')
param containerRegistryLoginServer string

@description('Resource id of the identity the app runs as, for the image pull.')
param identityResourceId string

@description('Client id of that identity. DefaultAzureCredential needs it whenever more than one identity is visible to the process, and inside a Container App more than one always is.')
param identityClientId string

@description('Name of that identity. It is also the PostgreSQL role name, because that is the name the bootstrap job creates it under.')
param identityName string

@description('Principal id of that identity. The bootstrap job names it by object id rather than by display name, which is not unique among service principals.')
param identityPrincipalId string

@description('Resource id of the administration identity the bootstrap job runs as.')
param administrationIdentityResourceId string

@description('Client id of that identity, so DefaultAzureCredential picks it and not the workload\'s.')
param administrationIdentityClientId string

@description('Name of that identity, which is its PostgreSQL role name. It is a database administrator; the workload is deliberately not.')
param administrationIdentityName string

@description('''
Fully-qualified image reference, registry included.

Not defaulted. A Container App whose image does not exist fails its first
revision and leaves an app that is deployed and broken, which is worse than a
deployment that refuses to start: publish.sh builds the image and prints this
value, and deploy.sh reads it from the registry when it is not set.
''')
@minLength(3)
param apiImage string

@description('Microsoft Entra tenant the API validates tokens from.')
@minLength(1)
param tenantId string

@description('''
Expected `aud` claim, which is the application id URI of the app registration
clients ask for a token for.

Required, and it is the one prerequisite this template cannot create: an Entra
app registration is not an ARM resource. A deployed environment refuses the
development identity provider (there is no bypass to fall back to), so an
audience has to exist before anything can be called.
''')
@minLength(1)
param apiAudience string

@description('Name of the PostgreSQL server to connect to.')
param databaseHost string

@description('Name of the database.')
param databaseName string

@description('Azure OpenAI endpoint.')
param openaiEndpoint string

@description('Chat deployment name.')
param chatDeploymentName string

@description('Embedding deployment name.')
param embeddingDeploymentName string

@description('Azure AI Search endpoint.')
param searchEndpoint string

@description('''
OTLP/HTTP address of the collector, or empty to emit nothing.

Empty is not a degraded mode: the platform's own no-op tracer costs almost
nothing and keeps `if tracing_enabled:` out of the code doing the work
(ADR-0025). What is refused is tracing switched on with nowhere to send, because
that looks exactly like tracing that works until somebody opens the backend.
''')
param collectorEndpoint string = ''

@description('''
Price list for cost attribution, as `{ "<model id>": { "input": <per million>, "output": <per million> } }`.

Empty by default, and empty means no cost measurement rather than a cost of zero
— zero is a claim, and the honest answer when a model is unpriced is silence.
Nothing here is derived from Azure: cost is token counts multiplied by a table
somebody typed, the invoice is the authority, and inventing the table in a
template would be the fastest way to produce a confident wrong number.
''')
param modelPrices object = {}

@description('Currency the prices above are in. Recorded on every measurement, because a chart mixing two currencies is worse than no chart.')
param priceCurrency string = 'USD'

@description('A label for that price list — a date will do. Required as soon as there are prices: a cost figure that cannot be traced back to the table that produced it is uninterpretable the moment the table changes.')
param priceRevision string = 'unset'

@description('''
Replicas to keep running when nothing is being asked of it.

Zero by default, which is the right answer for an environment that exists to be
measured and destroyed: it costs nothing while idle. The cost is a cold start on
the first request after five minutes of quiet — a container pull, a process
start and a pool opening — which is worth knowing about before it is measured
rather than after.
''')
@minValue(0)
param minReplicas int = 0

@description('Ceiling on replicas. Three rather than the default ten: this environment is bounded by a budget rather than by demand, and a runaway scale-out is the expensive failure available here.')
@minValue(1)
param maxReplicas int = 3

@description('Concurrent requests per replica before another is added. Low, because the expensive requests here spend their time waiting on a model rather than on CPU.')
@minValue(1)
param concurrentRequests int = 20

@description('vCPU per replica. The sidecar below takes a share of this, so it is sized for two containers rather than one.')
param apiCpu string = '1.0'

@description('Memory per replica. Container Apps fixes the ratio to 2 GiB per vCPU on the Consumption profile and rejects anything else.')
param apiMemory string = '2Gi'

@description('Tags applied to every resource.')
param tags object

var appName = 'ca-paimon-api-${environmentName}'
var jobName = 'cj-paimon-migrate-${environmentName}'
var bootstrapName = 'cj-paimon-bootstrap-${environmentName}'

// Known before the app exists, because both halves are: the name is ours and the
// domain belongs to the environment. That is what makes it possible to tell the
// application its own public address at deployment time, which it needs for two
// things that are otherwise a second deployment — see the MCP settings below.
var apiUrl = 'https://${appName}.${containerAppsDefaultDomain}'
var apiHost = '${appName}.${containerAppsDefaultDomain}'

// Telemetry goes to the collector or nowhere. Both signals are named explicitly
// rather than sharing a base URL: several backends accept OTLP traces and not
// OTLP metrics, and pointing metrics at a traces endpoint fails quietly — an
// empty dashboard that looks like a platform emitting nothing.
var telemetryEnvironment = empty(collectorEndpoint)
  ? []
  : [
      {
        name: 'PAIMON_OBSERVABILITY__TRACING__ENABLED'
        value: 'true'
      }
      {
        name: 'PAIMON_OBSERVABILITY__TRACING__ENDPOINT'
        value: '${collectorEndpoint}/v1/traces'
      }
      {
        name: 'PAIMON_OBSERVABILITY__METRICS__ENABLED'
        value: 'true'
      }
      {
        name: 'PAIMON_OBSERVABILITY__METRICS__ENDPOINT'
        value: '${collectorEndpoint}/v1/metrics'
      }
    ]

var pricingEnvironment = empty(modelPrices)
  ? []
  : [
      {
        name: 'PAIMON_OBSERVABILITY__METRICS__PRICING__CURRENCY'
        value: priceCurrency
      }
      {
        name: 'PAIMON_OBSERVABILITY__METRICS__PRICING__REVISION'
        value: priceRevision
      }
      {
        // Pydantic parses a nested mapping from JSON, so the object is passed
        // through as written rather than flattened into one variable per model.
        name: 'PAIMON_OBSERVABILITY__METRICS__PRICING__MODELS'
        value: string(modelPrices)
      }
    ]

// The API and the migration job are the same image with different commands and
// need most of the same configuration. Written once.
var databaseEnvironment = [
  {
    name: 'PAIMON_DATABASE__HOST'
    value: databaseHost
  }
  {
    name: 'PAIMON_DATABASE__NAME'
    value: databaseName
  }
  {
    // The managed identity's name, which is also the PostgreSQL role the
    // bootstrap job creates for it. If those two disagree the symptom is an
    // authentication failure that says nothing whatsoever about names.
    name: 'PAIMON_DATABASE__USER'
    value: identityName
  }
  {
    name: 'PAIMON_DATABASE__AUTH'
    value: 'entra'
  }
  {
    name: 'AZURE_CLIENT_ID'
    value: identityClientId
  }
]

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: containerAppsEnvironmentName
}

resource api 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityResourceId}': {}
    }
  }
  properties: {
    environmentId: containerAppsEnvironment.id
    configuration: {
      // Empty, and it is the point. Every credential this application would
      // otherwise hold — model keys, a search key, a database password — was
      // removed by ADR-0014, ADR-0037 and ADR-0038 respectively. An empty
      // secrets array is a claim that can be checked by reading one line.
      secrets: []
      registries: [
        {
          server: containerRegistryLoginServer
          // The identity pulls the image. The AcrPull assignment for it is made
          // in the platform module, where the identity is created, precisely so
          // that it exists before anything tries to pull.
          identity: identityResourceId
        }
      ]
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
        // There is no request-timeout property to set here. Container Apps
        // terminates an idle ingress connection at 240 seconds and that number is
        // the platform's, not a default to raise: an agent run that takes longer
        // has its client disconnected by the infrastructure rather than by the
        // application. It is a real limit of this hosting choice (ADR-0034), and
        // the answer to it is streaming or a job, not a bigger number.
        clientCertificateMode: 'ignore'
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      // Single revision. Blue-green needs two revisions and a traffic split, and
      // that is a delivery concern with a phase of its own; running it here would
      // mean a rollback story written before there is anything to roll back.
      activeRevisionsMode: 'Single'
      maxInactiveRevisions: 2
    }
    template: {
      containers: [
        {
          name: 'api'
          image: apiImage
          resources: {
            cpu: json(apiCpu)
            memory: apiMemory
          }
          env: concat(databaseEnvironment, [
            {
              name: 'PAIMON_ENVIRONMENT'
              value: 'production'
            }
            {
              // The cache is the container beside this one. See ADR-0039 for why
              // it is not a managed service, and for what would change that.
              name: 'PAIMON_REDIS__HOST'
              value: '127.0.0.1'
            }
            {
              name: 'PAIMON_AUTH__PROVIDER'
              value: 'entra'
            }
            {
              name: 'PAIMON_AUTH__TENANT_ID'
              value: tenantId
            }
            {
              name: 'PAIMON_AUTH__AUDIENCE'
              value: apiAudience
            }
            {
              name: 'PAIMON_EMBEDDING__PROVIDER'
              value: 'azure'
            }
            {
              name: 'PAIMON_CHAT__PROVIDER'
              value: 'azure'
            }
            {
              name: 'PAIMON_AZURE_OPENAI__ENDPOINT'
              value: openaiEndpoint
            }
            {
              name: 'PAIMON_AZURE_OPENAI__CHAT_DEPLOYMENT'
              value: chatDeploymentName
            }
            {
              name: 'PAIMON_AZURE_OPENAI__EMBEDDING_DEPLOYMENT'
              value: embeddingDeploymentName
            }
            {
              name: 'PAIMON_AZURE_SEARCH__ENDPOINT'
              value: searchEndpoint
            }
            {
              // The address clients actually reach this server at, which is what
              // RFC 8707 says the token audience has to be. Left unset since
              // Phase 4 because nothing knew the public address until there was
              // one; it is computed above from a name this template chooses and a
              // domain the environment owns, so it is knowable before the app
              // exists rather than after the first deployment.
              name: 'PAIMON_MCP__RESOURCE_URL'
              value: '${apiUrl}/mcp'
            }
            {
              // DNS-rebinding protection: the transport answers to these Host
              // values and no others. Behind an ingress that terminates TLS, the
              // only correct value is the public hostname — the container's own
              // name would let a rebinding attempt through and the localhost
              // defaults would refuse every real request.
              name: 'PAIMON_MCP__ALLOWED_HOSTS'
              value: '["${apiHost}"]'
            }
            {
              name: 'PAIMON_MCP__AUTHORIZATION_SERVER'
              // Via environment() rather than written out: the same template
              // deployed into a sovereign cloud would otherwise point every
              // client at an authority that does not serve that cloud.
              value: '${environment().authentication.loginEndpoint}${tenantId}/v2.0'
            }
          ], telemetryEnvironment, pricingEnvironment)
          probes: [
            {
              // Startup, not liveness, does the waiting. A liveness probe
              // generous enough to cover a cold start is a liveness probe that
              // takes minutes to notice a hung process; separating them lets each
              // one be tight about the thing it actually measures.
              type: 'Startup'
              httpGet: {
                path: '/api/v1/health/ready'
                port: 8000
              }
              periodSeconds: 5
              timeoutSeconds: 5
              // Five minutes. Long, because the first start after a deployment
              // opens two connection pools against a database that may still be
              // propagating a role assignment, and a container killed halfway
              // through that restarts into the same wait.
              failureThreshold: 60
            }
            {
              type: 'Liveness'
              httpGet: {
                // Deliberately the endpoint that touches nothing. A liveness
                // probe wired to readiness turns a database blip into a restart
                // loop, which empties the pools that were about to recover.
                path: '/api/v1/health/live'
                port: 8000
              }
              periodSeconds: 10
              timeoutSeconds: 3
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/api/v1/health/ready'
                port: 8000
              }
              periodSeconds: 10
              timeoutSeconds: 5
              failureThreshold: 3
              successThreshold: 1
            }
          ]
        }
        {
          // ADR-0039. Redis holds an embedding cache and nothing that cannot be
          // recomputed, so it runs beside the process that uses it: no ingress,
          // no address outside this replica, and therefore no credential to
          // protect a cache with. It is deliberately not persisted.
          name: 'cache'
          image: 'docker.io/library/redis:7.4-alpine'
          command: ['redis-server']
          args: [
            // Memory-bounded and told what to do when it fills, rather than
            // allowed to grow until the replica is killed for it. The eviction
            // policy is the honest one for a cache: drop the least recently used
            // key. A cache that refuses writes when full is a cache that turns
            // into an outage.
            '--maxmemory'
            '256mb'
            '--maxmemory-policy'
            'allkeys-lru'
            // No append-only file and no RDB snapshots. There is nothing here
            // worth surviving a restart, and writing it to disk would only buy
            // slower writes and a corrupt file to recover.
            '--save'
            ''
            '--appendonly'
            'no'
          ]
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          probes: [
            {
              type: 'Liveness'
              tcpSocket: {
                port: 6379
              }
              periodSeconds: 10
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          {
            name: 'http'
            http: {
              metadata: {
                concurrentRequests: '${concurrentRequests}'
              }
            }
          }
        ]
      }
    }
  }
}

// ---------------------------------------------------------------------------
// Schema changes
// ---------------------------------------------------------------------------

// Everything a job needs that is not the database. Settings are validated as a
// whole, so a job needs every value the API needs even where it reaches none of
// those services: they have to be present and well-formed. Written once, because
// two jobs need the same set and a second copy drifts from the first.
var jobEnvironment = [
          {
            name: 'PAIMON_ENVIRONMENT'
            value: 'production'
          }
          {
            // Settings are validated as a whole even here, so the job needs the
            // values the API needs. It reaches none of these services; it needs
            // them to be present and well-formed.
            name: 'PAIMON_REDIS__HOST'
            value: '127.0.0.1'
          }
          {
            name: 'PAIMON_AUTH__PROVIDER'
            value: 'entra'
          }
          {
            name: 'PAIMON_AUTH__TENANT_ID'
            value: tenantId
          }
          {
            name: 'PAIMON_AUTH__AUDIENCE'
            value: apiAudience
          }
          {
            name: 'PAIMON_EMBEDDING__PROVIDER'
            value: 'azure'
          }
          {
            name: 'PAIMON_CHAT__PROVIDER'
            value: 'azure'
          }
          {
            name: 'PAIMON_AZURE_OPENAI__ENDPOINT'
            value: openaiEndpoint
          }
          {
            name: 'PAIMON_AZURE_OPENAI__CHAT_DEPLOYMENT'
            value: chatDeploymentName
          }
          {
            name: 'PAIMON_AZURE_OPENAI__EMBEDDING_DEPLOYMENT'
            value: embeddingDeploymentName
          }
          {
            name: 'PAIMON_AZURE_SEARCH__ENDPOINT'
            value: searchEndpoint
          }
]
// Run as the administration identity, not the workload's, so three values differ
// from every other container here: which identity is attached, which one
// DefaultAzureCredential picks, and which role name the connection presents.
// Overridden rather than parameterised, because everything else — image, host,
// database — genuinely is the same. A later entry wins in concat().
var administrationEnvironment = concat(databaseEnvironment, [
  {
    name: 'PAIMON_DATABASE__USER'
    value: administrationIdentityName
  }
  {
    name: 'AZURE_CLIENT_ID'
    value: administrationIdentityClientId
  }
])

resource bootstrap 'Microsoft.App/jobs@2024-03-01' = {
  name: bootstrapName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${administrationIdentityResourceId}': {}
    }
  }
  properties: {
    environmentId: containerAppsEnvironment.id
    configuration: {
      triggerType: 'Manual'
      // Short, unlike the migration's. This opens one connection and runs five
      // statements; one still running after five minutes cannot reach the
      // database, and waiting half an hour to be told that helps nobody.
      replicaTimeout: 300
      replicaRetryLimit: 0
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      secrets: []
      registries: [
        {
          server: containerRegistryLoginServer
          identity: administrationIdentityResourceId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'bootstrap'
          image: apiImage
          command: ['python']
          args: [
            '-m'
            'paimon.interfaces.cli.bootstrap_database'
            '--role'
            identityName
            '--object-id'
            identityPrincipalId
          ]
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: concat(administrationEnvironment, jobEnvironment)
        }
      ]
    }
  }
}

resource migrate 'Microsoft.App/jobs@2024-03-01' = {
  name: jobName
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityResourceId}': {}
    }
  }
  properties: {
    environmentId: containerAppsEnvironment.id
    configuration: {
      // Manual. Not a schedule, and not something that runs on every deployment:
      // a migration is a decision, and the moment it stops being one is the
      // moment a bad one runs unattended at three in the morning. Phase 8 wires
      // it into delivery, which is where a gate can be put in front of it.
      triggerType: 'Manual'
      replicaTimeout: 1800
      replicaRetryLimit: 0
      manualTriggerConfig: {
        parallelism: 1
        replicaCompletionCount: 1
      }
      secrets: []
      registries: [
        {
          server: containerRegistryLoginServer
          identity: identityResourceId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'migrate'
          image: apiImage
          // The same image as the API, deliberately. A migration run from a
          // different image is a migration run against a different definition of
          // the schema, and the difference shows up as drift nobody can explain.
          command: ['alembic']
          args: ['upgrade', 'head']
          // No working directory is set because none can be: Container Apps has
          // no such property. It does not need one — the image's WORKDIR is
          // /app, which is where alembic.ini and the migrations now live, and
          // alembic.ini names its script_location relative to wherever alembic
          // was started.
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: concat(databaseEnvironment, jobEnvironment)
        }
      ]
    }
  }
}

@description('Name of the bootstrap job, which creates the workload role and the extensions. It runs once per environment, before the migration.')
output bootstrapJobName string = bootstrap.name

@description('Public address of the API.')
output apiUrl string = apiUrl

@description('Hostname the API answers to, which is also the only Host value the MCP transport accepts.')
output apiFqdn string = apiHost

@description('Name of the container app.')
output apiName string = api.name

@description('Name of the migration job, which migrate.sh starts.')
output migrationJobName string = migrate.name

@description('Image both of them run.')
output apiImageDeployed string = apiImage
