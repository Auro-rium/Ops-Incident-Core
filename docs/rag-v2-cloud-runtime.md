# Cloud-Only GPU RAG Runtime

IncidentOps Core and Collector do not load embedding or reranking models. The
only model runtime is `model_runtime/`, deployed to Azure ML managed GPU online
endpoints.

## Runtime boundaries

```text
Collector -> Core document batch API -> Core indexer -> Azure ML embedder
Core search -> Azure ML embedder -> PostgreSQL vector + FTS -> Azure ML reranker
Core answer -> Azure OpenAI / Foundry
```

The Collector stays deterministic: discovery, path policy, redaction, metadata,
normalization, checkpointing, and sync. It never embeds, reranks, retrieves, or
answers questions.

## Required deployment settings

```text
APP_ENV=production
RAG_RETRIEVAL_VERSION=v2
RAG_INDEX_VERSION=v2
EMBEDDING_MODEL=azure-ml-bge-m3
RERANKER_MODEL=azure-ml-bge-reranker-v2-m3
RAG_GPU_ENDPOINT_REQUIRED=true
RAG_EMBEDDING_DIM=1024
RAG_MODEL_REVISION=<same immutable embedding model revision used by the endpoint>
RAG_EMBEDDING_ENDPOINT=<Azure ML embedding scoring URI>
RAG_RERANKER_ENDPOINT=<Azure ML reranker scoring URI>
RAG_REMOTE_AUTH_MODE=managed_identity
RAG_RERANK_MODE=conditional
RAG_PARALLEL_RETRIEVAL=true
RAG_ASYNC_INDEXING=true
RAG_INDEX_MAX_RETRIES=3
RAG_CACHE_ENABLED=true
RAG_CACHE_TTL_SECONDS=3600
```

The preferred authentication path is the Core Container App managed identity
with permission to invoke Azure ML endpoints. An API key is only supported for
an explicitly configured non-production integration; it must be stored in Key
Vault and never appear in a repository, log, or diagnostic payload.

## Network prerequisite

The managed endpoint manifest disables public network access. Therefore the
Core API and worker Container Apps must be deployed into a VNet-integrated
Container Apps environment with private DNS and private-endpoint connectivity
to the Azure ML workspace. The current budget-demo Bicep does **not** yet
provision that private network path. Do not enable GPU RAG v2 in that topology
until the VNet/private endpoint deployment is added and a bounded scoring smoke
from the worker succeeds.

## Deploy

The workflow `.github/workflows/deploy-rag-models.yml` is manual-only. It builds
the model image without downloading model weights, then optionally deploys the
Azure ML managed endpoints and runs the cloud benchmark job.

```bash
bash scripts/azure_build_rag_runtime.sh
bash scripts/azure_deploy_rag_models.sh
bash scripts/azure_rag_eval.sh
```

Required GitHub configuration:

```text
AZURE_CLIENT_ID
AZURE_TENANT_ID
AZURE_SUBSCRIPTION_ID
AZURE_RESOURCE_GROUP
AZURE_ML_WORKSPACE
ACR_NAME
BGE_M3_REVISION
BGE_RERANKER_REVISION
```

## Verification

No real RAG models or end-to-end RAG tests run on a laptop. Verification is
cloud-only:

1. Azure ML deployment reaches `Succeeded`.
2. A bounded embedding request reports 1024 dimensions.
3. A bounded rerank request returns one score per candidate.
4. Core v2 indexing publishes `chunk_embeddings` rows.
5. Search returns v2-vector evidence with lexical fusion.
6. The benchmark job records retrieval, reranking, latency, and token metrics.

`BGE_M3_REVISION` and `BGE_RERANKER_REVISION` must be immutable Hugging Face
commit identifiers. The deployment command refuses the mutable `main` default.

## Async indexing delivery

The batch API persists a redacted `index_jobs` row before returning. Workers
scan Postgres for pending or abandoned queued jobs, publish only the durable job
ID to Redis Streams, and use row locks to prevent duplicate publication from
creating duplicate chunks. Failed jobs retry up to `RAG_INDEX_MAX_RETRIES` and
then remain explicitly failed with a safe error code. Redis does not carry raw
document content.

## Measured rollout gates

Do not enable `RAG_RETRIEVAL_VERSION=v2` for all traffic until cloud evals show:

| Gate | Target |
|---|---:|
| Embedding response dimension | 1024 |
| Code/API source-type hit rate | >= 0.70 |
| Search p95 | < 1.5 s |
| Simple direct-evidence answer | < 2 s |
| Normal synthesized answer p50 | < 8 s |
| Duplicate chunks after reingest | 0 |

These are rollout targets, not results claimed by this repository. Record real
Azure benchmark values before declaring the GPU path production-ready.

## Rollback

Set `RAG_RETRIEVAL_VERSION=v1`, restart Core API and workers, and retain v2
embeddings for later comparison. Do not delete v2 data until v1 fallback has
been verified.
