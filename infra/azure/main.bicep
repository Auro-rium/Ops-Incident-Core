targetScope = 'resourceGroup'

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Short lowercase prefix used for resource names. Keep under 12 chars for globally named resources.')
param namePrefix string = 'incidentops'

@description('Existing or desired Azure Container Registry name. Leave empty to generate one.')
param acrName string = ''

@description('Environment label.')
param environmentName string = 'demo'

@description('Core API image tag already pushed to ACR.')
param coreImageTag string = 'latest'

@description('Collector image tag already pushed to ACR.')
param collectorImageTag string = 'latest'

@description('Frontend image tag already pushed to ACR.')
param frontendImageTag string = 'latest'

@description('Whether to deploy the frontend Container App.')
param deployFrontend bool = false

@secure()
@description('PostgreSQL admin password.')
param postgresAdminPassword string

@description('PostgreSQL admin user.')
param postgresAdminUser string = 'incidentops'

@secure()
@description('JWT signing secret. Must be non-default and at least 32 chars.')
param jwtSecret string

@description('Bootstrap admin email.')
param bootstrapAdminEmail string

@secure()
@description('Bootstrap admin password.')
param bootstrapAdminPassword string

@secure()
@description('Collector API token. Use a Core access token for the demo collector.')
param incidentopsToken string = ''

@description('Core project id the demo collector should sync into. Leave blank until bootstrap/smoke creates one.')
param incidentopsProjectId string = ''

@description('Optional Git repository URL for the private Collector daemon. Empty keeps the daemon scaled to zero.')
param collectorRepoUrl string = ''

@description('Allowed browser origins for Core CORS.')
param corsOrigins string = 'https://CHANGE-ME'

@description('Azure OpenAI / Foundry endpoint. Required for production deployment.')
param azureOpenAIEndpoint string = ''

@secure()
@description('Azure OpenAI / Foundry key. Required for production deployment.')
param azureOpenAIApiKey string = ''

@description('Optional Azure OpenAI API version.')
param azureOpenAIApiVersion string = '2024-10-21'

@description('Azure OpenAI / Foundry chat deployment name. Required for production deployment.')
param azureOpenAIChatDeployment string = ''

@description('Azure OpenAI / Foundry embedding deployment name. Required for production deployment.')
param azureOpenAIEmbeddingDeployment string = ''

@description('Qdrant HTTPS endpoint reachable from the Core API and worker.')
param qdrantUrl string = ''

@secure()
@description('Qdrant API key. Store the supplied value in Key Vault.')
param qdrantApiKey string = ''

@description('Qdrant collection name used by Core.')
param qdrantCollection string = 'incidentops_chunks'

@description('Embedding vector dimension configured for the selected embedding deployment.')
param embeddingDimension int = 1024

@secure()
@description('Core access token used by the MCP server to call Core APIs. Set after bootstrap or during redeploy.')
param incidentopsMcpToken string = ''

var suffix = uniqueString(resourceGroup().id, namePrefix, environmentName)
var safePrefix = toLower(replace(namePrefix, '-', ''))
var effectiveAcrName = empty(acrName) ? take('${safePrefix}${suffix}', 50) : acrName
var logName = '${namePrefix}-${environmentName}-logs'
var appEnvName = '${namePrefix}-${environmentName}-apps'
var identityName = '${namePrefix}-${environmentName}-apps-mi'
var vaultName = take('${safePrefix}-${environmentName}-${suffix}', 24)
var postgresName = take('${safePrefix}-${environmentName}-${suffix}', 63)
var redisName = take('${safePrefix}-${environmentName}-${suffix}', 63)
var databaseName = 'incidentops'
var databaseUrl = 'postgresql+asyncpg://${postgresAdminUser}:${postgresAdminPassword}@${postgresServer.properties.fullyQualifiedDomainName}:5432/${databaseName}?ssl=require'
var coreApiImage = '${containerRegistry.properties.loginServer}/incidentops-core:${coreImageTag}'
var coreWorkerImage = '${containerRegistry.properties.loginServer}/incidentops-core:${coreImageTag}'
var coreMcpImage = '${containerRegistry.properties.loginServer}/incidentops-core:${coreImageTag}'
var collectorImage = '${containerRegistry.properties.loginServer}/incidentops-core:${collectorImageTag}'
var frontendImage = '${containerRegistry.properties.loginServer}/incidentops-frontend:${frontendImageTag}'
var apiContainerAppName = '${namePrefix}-core-api'
var workerContainerAppName = '${namePrefix}-core-worker'
var mcpContainerAppName = '${namePrefix}-mcp'
var collectorContainerAppName = '${namePrefix}-collector'
var frontendContainerAppName = '${namePrefix}-frontend'
var benchmarkJobName = '${namePrefix}-benchmark-job'

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: effectiveAcrName
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
  }
}

resource appIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: vaultName
  location: location
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enabledForTemplateDeployment: true
    publicNetworkAccess: 'Enabled'
    softDeleteRetentionInDays: 7
  }
}

resource postgresServer 'Microsoft.DBforPostgreSQL/flexibleServers@2023-12-01-preview' = {
  name: postgresName
  location: location
  sku: {
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    administratorLogin: postgresAdminUser
    administratorLoginPassword: postgresAdminPassword
    storage: {
      storageSizeGB: 32
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    network: {
      publicNetworkAccess: 'Enabled'
    }
  }
}

resource postgresDatabase 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2023-12-01-preview' = {
  parent: postgresServer
  name: databaseName
  properties: {}
}

resource postgresAllowAzureServices 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2023-12-01-preview' = {
  parent: postgresServer
  name: 'allow-azure-services'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

resource redisCache 'Microsoft.Cache/redis@2024-03-01' = {
  name: redisName
  location: location
  properties: {
    sku: {
      name: 'Basic'
      family: 'C'
      capacity: 0
    }
    enableNonSslPort: false
    minimumTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
  }
}

resource databaseUrlSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'DATABASE-URL'
  properties: {
    value: databaseUrl
  }
}

resource redisPasswordSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'REDIS-PASSWORD'
  properties: {
    value: redisCache.listKeys().primaryKey
  }
}

resource jwtSecretResource 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'JWT-SECRET'
  properties: {
    value: jwtSecret
  }
}

resource bootstrapEmailSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'BOOTSTRAP-ADMIN-EMAIL'
  properties: {
    value: bootstrapAdminEmail
  }
}

resource bootstrapPasswordSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'BOOTSTRAP-ADMIN-PASSWORD'
  properties: {
    value: bootstrapAdminPassword
  }
}

resource collectorTokenSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'INCIDENTOPS-TOKEN'
  properties: {
    value: empty(incidentopsToken) ? 'disabled' : incidentopsToken
  }
}

resource azureOpenAIKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'AZURE-OPENAI-API-KEY'
  properties: {
    value: empty(azureOpenAIApiKey) ? 'disabled' : azureOpenAIApiKey
  }
}

resource qdrantApiKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'QDRANT-API-KEY'
  properties: {
    value: empty(qdrantApiKey) ? 'disabled' : qdrantApiKey
  }
}

resource mcpTokenSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'INCIDENTOPS-MCP-TOKEN'
  properties: {
    value: empty(incidentopsMcpToken) ? 'disabled' : incidentopsMcpToken
  }
}

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: appEnvName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

var acrPullRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
var keyVaultSecretsUserRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')

resource acrPullAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(containerRegistry.id, appIdentity.id, 'acr-pull')
  scope: containerRegistry
  properties: {
    roleDefinitionId: acrPullRoleId
    principalId: appIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource keyVaultSecretsAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, appIdentity.id, 'key-vault-secrets-user')
  scope: keyVault
  properties: {
    roleDefinitionId: keyVaultSecretsUserRoleId
    principalId: appIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

var sharedCoreEnv = [
  {
    name: 'APP_ENV'
    value: 'production'
  }
  {
    name: 'DB_CREATE_ALL'
    value: 'false'
  }
  {
    name: 'DB_REQUIRE_MIGRATIONS'
    value: 'true'
  }
  {
    name: 'LOCAL_INGEST_ENABLED'
    value: 'false'
  }
  {
    name: 'ENABLE_LOCAL_INGEST'
    value: 'false'
  }
  {
    name: 'ALLOW_DEMO_PROJECT_BYPASS'
    value: 'false'
  }
  {
    name: 'ALLOW_LOCAL_SEED_ADMIN'
    value: 'false'
  }
  {
    name: 'DEMO_MODE_PUBLIC'
    value: 'false'
  }
  {
    name: 'WORKER_MODE'
    value: 'queue'
  }
  {
    name: 'JOB_QUEUE_BACKEND'
    value: 'redis'
  }
  {
    name: 'RATE_LIMIT_BACKEND'
    value: 'redis'
  }
  {
    name: 'METRICS_BACKEND'
    value: 'prometheus'
  }
  {
    name: 'METRICS_PUBLIC'
    value: 'false'
  }
  {
    name: 'MCP_ENABLED'
    value: 'true'
  }
  {
    name: 'CORS_ALLOW_ORIGINS'
    value: corsOrigins
  }
  {
    name: 'CORS_ORIGINS'
    value: corsOrigins
  }
  {
    name: 'ALLOW_WILDCARD_CORS'
    value: 'false'
  }
  {
    name: 'EMBEDDING_MODEL'
    value: 'azure-openai'
  }
  {
    name: 'EMBEDDING_DIM'
    value: string(embeddingDimension)
  }
  {
    name: 'RETRIEVAL_BACKEND'
    value: 'qdrant'
  }
  {
    name: 'QDRANT_URL'
    value: qdrantUrl
  }
  {
    name: 'QDRANT_COLLECTION'
    value: qdrantCollection
  }
  {
    name: 'VECTOR_INDEX_VERSION'
    value: 'current'
  }
  {
    name: 'AZURE_OPENAI_ENDPOINT'
    value: azureOpenAIEndpoint
  }
  {
    name: 'AZURE_OPENAI_API_VERSION'
    value: azureOpenAIApiVersion
  }
  {
    name: 'AZURE_OPENAI_CHAT_DEPLOYMENT'
    value: azureOpenAIChatDeployment
  }
  {
    name: 'AZURE_OPENAI_EMBEDDING_DEPLOYMENT'
    value: azureOpenAIEmbeddingDeployment
  }
  {
    name: 'REQUIRE_AZURE_OPENAI'
    value: 'true'
  }
  {
    name: 'DATABASE_URL'
    secretRef: 'database-url'
  }
  {
    name: 'REDIS_HOST'
    value: redisCache.properties.hostName
  }
  {
    name: 'REDIS_PORT'
    value: '6380'
  }
  {
    name: 'REDIS_SSL'
    value: 'true'
  }
  {
    name: 'REDIS_DATABASE'
    value: '0'
  }
  {
    name: 'REDIS_PASSWORD'
    secretRef: 'redis-password'
  }
  {
    name: 'JWT_SECRET'
    secretRef: 'jwt-secret'
  }
  {
    name: 'BOOTSTRAP_ADMIN_EMAIL'
    secretRef: 'bootstrap-admin-email'
  }
  {
    name: 'BOOTSTRAP_ADMIN_PASSWORD'
    secretRef: 'bootstrap-admin-password'
  }
  {
    name: 'AZURE_OPENAI_API_KEY'
    secretRef: 'azure-openai-api-key'
  }
  {
    name: 'QDRANT_API_KEY'
    secretRef: 'qdrant-api-key'
  }
]

var mcpSecrets = concat(coreSecrets, [
  {
    name: 'incidentops-mcp-token'
    keyVaultUrl: mcpTokenSecret.properties.secretUri
    identity: appIdentity.id
  }
])

var coreSecrets = [
  {
    name: 'database-url'
    keyVaultUrl: databaseUrlSecret.properties.secretUri
    identity: appIdentity.id
  }
  {
    name: 'redis-password'
    keyVaultUrl: redisPasswordSecret.properties.secretUri
    identity: appIdentity.id
  }
  {
    name: 'jwt-secret'
    keyVaultUrl: jwtSecretResource.properties.secretUri
    identity: appIdentity.id
  }
  {
    name: 'bootstrap-admin-email'
    keyVaultUrl: bootstrapEmailSecret.properties.secretUri
    identity: appIdentity.id
  }
  {
    name: 'bootstrap-admin-password'
    keyVaultUrl: bootstrapPasswordSecret.properties.secretUri
    identity: appIdentity.id
  }
  {
    name: 'azure-openai-api-key'
    keyVaultUrl: azureOpenAIKeySecret.properties.secretUri
    identity: appIdentity.id
  }
  {
    name: 'qdrant-api-key'
    keyVaultUrl: qdrantApiKeySecret.properties.secretUri
    identity: appIdentity.id
  }
]

resource coreApi 'Microsoft.App/containerApps@2024-03-01' = {
  name: apiContainerAppName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${appIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppsEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [
        {
          server: containerRegistry.properties.loginServer
          identity: appIdentity.id
        }
      ]
      secrets: coreSecrets
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
    }
    template: {
      scale: {
        minReplicas: 1
        maxReplicas: 2
      }
      containers: [
        {
          name: 'api'
          image: coreApiImage
          command: [
            'sh'
            '-c'
            'python scripts/check_migrations.py && uvicorn apps.api.main:app --host 0.0.0.0 --port 8000'
          ]
          env: sharedCoreEnv
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }
      ]
    }
  }
  dependsOn: [
    acrPullAssignment
    keyVaultSecretsAssignment
    postgresDatabase
    postgresAllowAzureServices
  ]
}

resource coreWorker 'Microsoft.App/containerApps@2024-03-01' = {
  name: workerContainerAppName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${appIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppsEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [
        {
          server: containerRegistry.properties.loginServer
          identity: appIdentity.id
        }
      ]
      secrets: coreSecrets
    }
    template: {
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
      containers: [
        {
          name: 'worker'
          image: coreWorkerImage
          command: [
            'python'
            '-m'
            'incidentops.worker'
          ]
          env: sharedCoreEnv
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }
      ]
    }
  }
  dependsOn: [
    coreApi
  ]
}

resource coreMcp 'Microsoft.App/containerApps@2024-03-01' = {
  name: mcpContainerAppName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${appIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppsEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [
        {
          server: containerRegistry.properties.loginServer
          identity: appIdentity.id
        }
      ]
      secrets: mcpSecrets
      ingress: {
        external: false
        targetPort: 8080
        transport: 'http'
        allowInsecure: false
      }
    }
    template: {
      scale: {
        minReplicas: empty(incidentopsMcpToken) ? 0 : 1
        maxReplicas: 1
      }
      containers: [
        {
          name: 'mcp'
          image: coreMcpImage
          command: [
            'python'
            '-m'
            'incidentops.mcp.server'
          ]
          env: concat(sharedCoreEnv, [
            {
              name: 'MCP_TRANSPORT'
              value: 'streamable-http'
            }
            {
              name: 'MCP_HOST'
              value: '0.0.0.0'
            }
            {
              name: 'MCP_PORT'
              value: '8080'
            }
            {
              name: 'MCP_PATH'
              value: '/mcp'
            }
            {
              name: 'MCP_CORE_API_URL'
              value: 'https://${coreApi.properties.configuration.ingress.fqdn}'
            }
            {
              name: 'MCP_TOKEN'
              secretRef: 'incidentops-mcp-token'
            }
          ])
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
        }
      ]
    }
  }
  dependsOn: [
    coreApi
    keyVaultSecretsAssignment
  ]
}

resource frontend 'Microsoft.App/containerApps@2024-03-01' = if (deployFrontend) {
  name: frontendContainerAppName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${appIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppsEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [
        {
          server: containerRegistry.properties.loginServer
          identity: appIdentity.id
        }
      ]
      ingress: {
        external: true
        targetPort: 8080
        transport: 'auto'
        allowInsecure: false
      }
    }
    template: {
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
      containers: [
        {
          name: 'frontend'
          image: frontendImage
          env: [
            {
              name: 'NEXT_PUBLIC_INCIDENTOPS_API_URL'
              value: 'https://${coreApi.properties.configuration.ingress.fqdn}'
            }
            {
              name: 'NEXT_PUBLIC_API_BASE_URL'
              value: 'https://${coreApi.properties.configuration.ingress.fqdn}'
            }
            {
              name: 'NEXT_PUBLIC_APP_ENV'
              value: 'production'
            }
          ]
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
        }
      ]
    }
  }
  dependsOn: [
    acrPullAssignment
  ]
}

resource collector 'Microsoft.App/containerApps@2024-03-01' = {
  name: collectorContainerAppName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${appIdentity.id}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppsEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [
        {
          server: containerRegistry.properties.loginServer
          identity: appIdentity.id
        }
      ]
      secrets: [
        {
          name: 'incidentops-token'
          keyVaultUrl: collectorTokenSecret.properties.secretUri
          identity: appIdentity.id
        }
      ]
    }
    template: {
      scale: {
        minReplicas: empty(incidentopsToken) || empty(incidentopsProjectId) || empty(collectorRepoUrl) ? 0 : 1
        maxReplicas: 1
      }
      containers: [
        {
          name: 'collector'
          image: collectorImage
          command: [
            'python'
            '-m'
            'incidentops.collector'
            'daemon'
          ]
          env: [
            {
              name: 'INCIDENTOPS_API_URL'
              value: 'https://${coreApi.properties.configuration.ingress.fqdn}'
            }
            {
              name: 'INCIDENTOPS_TOKEN'
              secretRef: 'incidentops-token'
            }
            {
              name: 'PROJECT_ID'
              value: incidentopsProjectId
            }
            {
              name: 'INCIDENTOPS_PROJECT_ID'
              value: incidentopsProjectId
            }
            {
              name: 'SOURCE_NAME'
              value: 'azure-demo-source'
            }
            {
              name: 'SOURCE_TYPE'
              value: 'filesystem'
            }
            {
              name: 'COLLECTOR_REPO_URL'
              value: collectorRepoUrl
            }
            {
              name: 'COLLECTOR_ENVIRONMENT'
              value: 'azure-demo'
            }
          ]
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
        }
      ]
    }
  }
  dependsOn: [
    coreApi
    keyVaultSecretsAssignment
  ]
}


resource benchmarkJob 'Microsoft.App/jobs@2024-03-01' = {
  name: benchmarkJobName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${appIdentity.id}': {}
    }
  }
  properties: {
    environmentId: containerAppsEnvironment.id
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 7200
      replicaRetryLimit: 0
      registries: [
        {
          server: containerRegistry.properties.loginServer
          identity: appIdentity.id
        }
      ]
      secrets: [
        {
          name: 'incidentops-token'
          keyVaultUrl: collectorTokenSecret.properties.secretUri
          identity: appIdentity.id
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'benchmark'
          image: collectorImage
          command: [
            '/bin/sh'
            '-c'
            '''
set -eu
: "${INCIDENTOPS_API_URL:?INCIDENTOPS_API_URL is required}"
: "${INCIDENTOPS_PROJECT_ID:?INCIDENTOPS_PROJECT_ID is required}"
: "${INCIDENTOPS_TOKEN:?INCIDENTOPS_TOKEN is required}"
REPORT="/tmp/incidentops-benchmark-report.json"
WORKDIR="/tmp/incidentops-benchmark"
REPO_URL="${REPO_URL:-https://github.com/temporalio/temporal.git}"
SOURCE_NAME="${SOURCE_NAME:-temporal}"
MAX_FILES="${MAX_FILES:-1500}"
BATCH_SIZE="${BATCH_SIZE:-100}"
CHANGED_FILE_TARGET="${CHANGED_FILE_TARGET:-README.md}"
set -- benchmark \
  --repo-url "$REPO_URL" \
  --core-url "$INCIDENTOPS_API_URL" \
  --project-id "$INCIDENTOPS_PROJECT_ID" \
  --source-name "$SOURCE_NAME" \
  --output "$REPORT" \
  --workdir "$WORKDIR" \
  --max-files "$MAX_FILES" \
  --batch-size "$BATCH_SIZE" \
  --changed-file-target "$CHANGED_FILE_TARGET" \
  --include-path README.md \
  --include-path docs/** \
  --include-path api/** \
  --include-path proto/** \
  --include-path schema/** \
  --include-path service/** \
  --include-path common/** \
  --include-path temporal/** \
  --include-path cmd/** \
  --include-path config/** \
  --include-path develop/** \
  --exclude-path .git/** \
  --exclude-path .github/** \
  --exclude-path temporaltest/** \
  --exclude-path tools/** \
  --exclude-path bin/** \
  --exclude-path dist/** \
  --exclude-path coverage/**
for key in QUERY_1 QUERY_2 QUERY_3 QUERY_4 QUERY_5; do
  eval value="\${$key:-}"
  if [ -n "$value" ]; then
    set -- "$@" --query "$value"
  fi
done
python -m incidentops.collector benchmark --repo-url "$REPO_URL" --project-id "$INCIDENTOPS_PROJECT_ID" --source-name "$SOURCE_NAME" --max-files "$MAX_FILES" --batch-size "$BATCH_SIZE" --changed-file-target "$CHANGED_FILE_TARGET" --output "$REPORT"
python - "$REPORT" <<'INNERPY'
import json, sys
report = json.load(open(sys.argv[1], encoding='utf-8'))
print('IncidentOps Azure Benchmark Summary')
print(f"repo_url: {report.get('repo_url')}")
print(f"files_seen: {report.get('files_seen')}")
print(f"documents_synced: {report.get('documents_synced')}")
print(f"chunks_created: {report.get('chunks_created')}")
print(f"sync_status: {report.get('latest_core_sync_status')}")
print(f"repeat_sync_skipped_unchanged: {report.get('repeat_sync_skipped_unchanged')}")
print(f"changed_file_update_detected: {report.get('changed_file_update_detected')}")
print(f"duplicate_chunks_after_update: {report.get('duplicate_chunks_after_update')}")
print(f"search_pass: {report.get('search_pass')}")
for item in report.get('search_queries', []):
    print(f"query={item.get('query')!r} results={item.get('search_result_count')} paths={item.get('top_evidence_paths', [])[:3]}")
INNERPY
'''
          ]
          env: [
            {
              name: 'INCIDENTOPS_API_URL'
              value: 'https://${coreApi.properties.configuration.ingress.fqdn}'
            }
            {
              name: 'INCIDENTOPS_TOKEN'
              secretRef: 'incidentops-token'
            }
            {
              name: 'INCIDENTOPS_PROJECT_ID'
              value: incidentopsProjectId
            }
            {
              name: 'REPO_URL'
              value: 'https://github.com/temporalio/temporal.git'
            }
            {
              name: 'SOURCE_NAME'
              value: 'temporal'
            }
            {
              name: 'MAX_FILES'
              value: '1500'
            }
            {
              name: 'BATCH_SIZE'
              value: '100'
            }
            {
              name: 'CHANGED_FILE_TARGET'
              value: 'README.md'
            }
            {
              name: 'QUERY_1'
              value: 'Where is the history service implemented?'
            }
            {
              name: 'QUERY_2'
              value: 'Which parts of the Temporal repo are relevant to investigating workflow task latency?'
            }
          ]
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
        }
      ]
    }
  }
  dependsOn: [
    coreApi
    keyVaultSecretsAssignment
  ]
}

resource migrationJob 'Microsoft.App/jobs@2024-03-01' = {
  name: '${namePrefix}-core-migrate'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${appIdentity.id}': {}
    }
  }
  properties: {
    environmentId: containerAppsEnvironment.id
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 900
      replicaRetryLimit: 0
      registries: [
        {
          server: containerRegistry.properties.loginServer
          identity: appIdentity.id
        }
      ]
      secrets: coreSecrets
    }
    template: {
      containers: [
        {
          name: 'migrate'
          image: coreApiImage
          command: [
            'sh'
            '-c'
            'alembic upgrade head && python scripts/check_migrations.py'
          ]
          env: sharedCoreEnv
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
        }
      ]
    }
  }
  dependsOn: [
    acrPullAssignment
    keyVaultSecretsAssignment
    postgresDatabase
    postgresAllowAzureServices
  ]
}

resource bootstrapJob 'Microsoft.App/jobs@2024-03-01' = {
  name: '${namePrefix}-bootstrap-admin'
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${appIdentity.id}': {}
    }
  }
  properties: {
    environmentId: containerAppsEnvironment.id
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 600
      replicaRetryLimit: 0
      registries: [
        {
          server: containerRegistry.properties.loginServer
          identity: appIdentity.id
        }
      ]
      secrets: coreSecrets
    }
    template: {
      containers: [
        {
          name: 'bootstrap-admin'
          image: coreApiImage
          command: [
            'python'
            '-m'
            'incidentops.security.bootstrap_admin'
          ]
          env: sharedCoreEnv
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
        }
      ]
    }
  }
  dependsOn: [
    migrationJob
  ]
}

output acrLoginServer string = containerRegistry.properties.loginServer
output keyVaultName string = keyVault.name
output containerAppsEnvironmentName string = containerAppsEnvironment.name
output coreApiUrl string = 'https://${coreApi.properties.configuration.ingress.fqdn}'
output frontendUrl string = deployFrontend ? 'https://${frontend!.properties.configuration.ingress.fqdn}' : ''
output mcpAppName string = coreMcp.name
output collectorAppName string = collector.name
output benchmarkJobName string = benchmarkJob.name
output migrationJobName string = migrationJob.name
output bootstrapJobName string = bootstrapJob.name
output postgresServerName string = postgresServer.name
output redisName string = redisCache.name
