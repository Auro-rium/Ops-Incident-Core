targetScope = 'resourceGroup'

@description('Azure region for the benchmark job.')
param location string = resourceGroup().location

@description('Container Apps environment resource ID.')
param containerAppsEnvironmentId string

@description('Managed identity resource ID used for ACR pull and Key Vault secret resolution.')
param managedIdentityId string

@description('ACR login server, for example incidentopsacr.azurecr.io.')
param acrLoginServer string

@description('Core image tag containing the in-repository Collector command.')
param collectorImageTag string

@description('Core API URL.')
param incidentopsApiUrl string

@description('Project ID to sync benchmark data into.')
param incidentopsProjectId string

@description('Key Vault URI for Core access token used by Collector.')
param incidentopsTokenSecretUri string

@description('Benchmark job name.')
param benchmarkJobName string = 'incidentops-benchmark-job'

@description('Repository URL benchmarked by the Collector.')
param repoUrl string = 'https://github.com/temporalio/temporal.git'

@description('Core source name for benchmark documents.')
param sourceName string = 'temporal'

@description('Maximum discovered files to inspect/sync.')
param maxFiles string = '1500'

@description('Collector batch size.')
param batchSize string = '100'

@description('Changed-file target used by the idempotency/update check.')
param changedFileTarget string = 'README.md'

var collectorImage = '${acrLoginServer}/incidentops-core:${collectorImageTag}'

resource benchmarkJob 'Microsoft.App/jobs@2024-03-01' = {
  name: benchmarkJobName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${managedIdentityId}': {}
    }
  }
  properties: {
    environmentId: containerAppsEnvironmentId
    configuration: {
      triggerType: 'Manual'
      replicaTimeout: 7200
      replicaRetryLimit: 0
      registries: [
        {
          server: acrLoginServer
          identity: managedIdentityId
        }
      ]
      secrets: [
        {
          name: 'incidentops-token'
          keyVaultUrl: incidentopsTokenSecretUri
          identity: managedIdentityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'benchmark'
          image: collectorImage
          command: [
            'python'
            '-m'
            'incidentops.collector'
          ]
          args: [
            'benchmark'
            '--repo-url'
            repoUrl
            '--project-id'
            incidentopsProjectId
            '--source-name'
            sourceName
            '--max-files'
            maxFiles
            '--batch-size'
            batchSize
            '--changed-file-target'
            changedFileTarget
          ]
          env: [
            {
              name: 'INCIDENTOPS_API_URL'
              value: incidentopsApiUrl
            }
            {
              name: 'INCIDENTOPS_TOKEN'
              secretRef: 'incidentops-token'
            }
            {
              name: 'INCIDENTOPS_PROJECT_ID'
              value: incidentopsProjectId
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
}

output benchmarkJobName string = benchmarkJob.name
