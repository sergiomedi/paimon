metadata description = '''
Where the telemetry goes: an Application Insights component, and an OpenTelemetry
collector that is the only thing allowed to write to it.

Container Apps has a managed OpenTelemetry agent that would remove this file
entirely, and it was rejected for reasons written down in ADR-0041. The short
version is that it cannot send metrics to Application Insights, speaks only OTLP
over gRPC while this platform exports over HTTP, and is configured per
environment rather than per application.

The collector is a plain upstream image with a configuration file. No vendor SDK
enters the application, which is the whole point of ADR-0025: the backend stays a
destination rather than becoming a dependency.
'''

@description('Region to deploy into.')
param location string

@description('Name of this environment.')
param environmentName string

@description('Name of the Container Apps environment to run in.')
param containerAppsEnvironmentName string

@description('Resource id of the Log Analytics workspace this component stores its data in.')
param logAnalyticsWorkspaceId string

@description('Resource id of the identity the collector runs as.')
param identityResourceId string

@description('Client id of that identity, which the collector presents when asking for a token.')
param identityClientId string

@description('Principal id of that identity, for the role assignment below.')
param identityPrincipalId string

@description('''
Version of the OpenTelemetry collector.

Pinned, and contrib rather than core: the Azure Monitor exporter and the Azure
authentication extension both live in contrib. Versions 0.124.0 to 0.150.0 carry
an authentication bypass in the *server* side of that extension — this deployment
uses only its client side, and the pin is comfortably past the fix regardless.
''')
param collectorVersion string = '0.157.0'

@description('''
Replicas of the collector.

One rather than zero, which is the opposite of the choice made for the API. A
collector asleep when telemetry arrives loses exactly the spans of a cold start,
and those are the interesting ones; a collector scaled to zero mid-flush loses
whatever was in the batch. It costs about 0.01 EUR an hour to not have that
problem.
''')
@minValue(0)
param collectorReplicas int = 1

@description('Tags applied to every resource.')
param tags object

var collectorName = 'ca-paimon-otel-${environmentName}'

// Named because the name says what it publishes. Despite saying "Metrics", this
// is the role that authorizes ingestion of every telemetry type into an
// Application Insights component — Microsoft's own documentation calls that out,
// and it is the kind of thing worth writing down beside the GUID.
var monitoringMetricsPublisher = '3913510d-42f4-4e42-8a64-420c390055eb'

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: containerAppsEnvironmentName
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-paimon-${environmentName}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    // Workspace-based. The classic mode is retired, and this keeps telemetry in
    // the workspace that already holds the container logs, so a trace and the
    // log lines around it are queryable together rather than in two products.
    WorkspaceResourceId: logAnalyticsWorkspaceId
    IngestionMode: 'LogAnalytics'
    // The same argument as ADR-0037, applied to the last service in the
    // environment that still had a key path. An instrumentation key is not
    // usually called a credential, but anything holding one can write telemetry
    // into this resource — and telemetry nobody can account for is worse than
    // none. With this on, the key identifies the destination and a Microsoft
    // Entra token authorizes the write.
    DisableLocalAuth: true
  }
}

resource collectorPublishesTelemetry 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: insights
  name: guid(insights.id, identityResourceId, monitoringMetricsPublisher)
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      monitoringMetricsPublisher
    )
    principalId: identityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------------------
// The collector
// ---------------------------------------------------------------------------

// Delivered as an environment variable rather than a mounted file. The collector
// accepts `--config=env:NAME` natively, and the alternative — a file — means a
// storage account, a file share and an environment storage definition, which is
// three resources and a mount to hold eighty lines of YAML.
//
// The two values that vary are substituted below rather than left to the
// collector's own `${env:...}` expansion, so what is written here is what runs:
// one fewer layer to be wrong about when nothing arrives in the portal.
//
// It lives in infrastructure/collector.yaml rather than in a string here, so an
// editor lints it as YAML and scripts/check.sh parses it — a template that
// compiles cleanly around a malformed configuration file is a deployment that
// succeeds and a collector that crash-loops.

var collectorConfig = replace(
  replace(loadTextContent('../collector.yaml'), '__CLIENT_ID__', identityClientId),
  '__CONNECTION_STRING__',
  insights.properties.ConnectionString
)

resource collector 'Microsoft.App/containerApps@2024-03-01' = {
  name: collectorName
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
      secrets: []
      ingress: {
        // Internal. A collector on the public internet is an open telemetry
        // ingestion endpoint, and the first thing that finds it will fill the
        // workspace this platform is billed for.
        external: false
        targetPort: 4318
        transport: 'http'
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      activeRevisionsMode: 'Single'
    }
    template: {
      containers: [
        {
          name: 'collector'
          image: 'docker.io/otel/opentelemetry-collector-contrib:${collectorVersion}'
          args: ['--config=env:OTEL_CONFIG']
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            {
              name: 'OTEL_CONFIG'
              value: collectorConfig
            }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/'
                port: 13133
              }
              periodSeconds: 30
              failureThreshold: 3
            }
            {
              // Readiness on the same endpoint, which is honest about what it
              // can and cannot tell you: it says the collector is accepting
              // data, not that the exporter is managing to deliver it. An
              // exporter that cannot authenticate keeps the collector healthy
              // and drops everything, and the only place that shows up is the
              // collector's own logs.
              type: 'Readiness'
              httpGet: {
                path: '/'
                port: 13133
              }
              periodSeconds: 10
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: {
        minReplicas: collectorReplicas
        maxReplicas: collectorReplicas
      }
    }
  }
  dependsOn: [
    // Role assignments take minutes to propagate, and a collector that starts
    // before this one has landed spends that time failing to export. Ordering
    // does not remove the wait, but it stops the wait from starting late.
    collectorPublishesTelemetry
  ]
}

@description('OTLP/HTTP address of the collector, reachable only from inside this environment.')
output collectorEndpoint string = 'http://${collectorName}'

@description('Name of the collector app, for az containerapp logs — which is where an export failure shows up and nowhere else.')
output collectorName string = collector.name

@description('Name of the Application Insights component.')
output insightsName string = insights.name

@description('Resource id of the component, for pointing a dashboard or a workbook at it.')
output insightsId string = insights.id
