locals {
  name               = "${var.name_prefix}-${var.environment}"
  availability_zones = slice(data.aws_availability_zones.available.names, 0, var.availability_zone_count)

  common_environment = [
    { name = "APP_ENV", value = var.environment },
    { name = "CLOUD_PROVIDER", value = "aws" },
    { name = "AWS_REGION", value = var.aws_region },
    { name = "DB_CREATE_ALL", value = "false" },
    { name = "DB_REQUIRE_MIGRATIONS", value = "true" },
    { name = "ALLOW_LOCAL_SEED_ADMIN", value = "false" },
    { name = "DEMO_MODE_PUBLIC", value = "false" },
    { name = "ALLOW_DEMO_PROJECT_BYPASS", value = "false" },
    { name = "LOCAL_INGEST_ENABLED", value = "false" },
    { name = "LOCAL_INGEST_ALLOWED_ROOTS", value = "/disabled" },
    { name = "EVAL_CASES_ALLOWED_ROOTS", value = "/disabled" },
    { name = "ALLOW_LOCAL_MODEL_LOADING", value = "false" },
    { name = "WORKER_MODE", value = "queue" },
    { name = "JOB_QUEUE_BACKEND", value = "redis" },
    { name = "RATE_LIMIT_BACKEND", value = "redis" },
    { name = "METRICS_BACKEND", value = "prometheus" },
    { name = "METRICS_PUBLIC", value = "false" },
    { name = "CORS_ALLOW_ORIGINS", value = var.cors_origins },
    { name = "ALLOW_WILDCARD_CORS", value = "false" },
    { name = "RETRIEVAL_BACKEND", value = "qdrant" },
    { name = "QDRANT_URL", value = "http://qdrant.${aws_service_discovery_private_dns_namespace.internal.name}:6333" },
    { name = "QDRANT_COLLECTION", value = var.qdrant_collection },
    { name = "VECTOR_INDEX_VERSION", value = var.vector_index_version },
    { name = "EMBEDDING_MODEL", value = "aws-bedrock-titan-v2" },
    { name = "EMBEDDING_DIM", value = tostring(var.embedding_dimension) },
    { name = "REQUIRE_AZURE_OPENAI", value = "false" },
    { name = "REQUIRE_AWS_MODELS", value = "true" },
    { name = "BEDROCK_CHAT_MODEL_ID", value = var.bedrock_chat_model_id },
    { name = "BEDROCK_EMBEDDING_MODEL_ID", value = var.bedrock_embedding_model_id },
    { name = "RAG_GPU_ENDPOINT_REQUIRED", value = tostring(var.enable_sagemaker_reranker) },
    { name = "SAGEMAKER_RERANKER_ENDPOINT_NAME", value = local.reranker_endpoint_name },
    { name = "RERANKER_MODEL", value = var.reranker_model_id },
    { name = "RAG_MODEL_REVISION", value = var.reranker_model_revision },
    { name = "RAG_RERANK_MODE", value = var.enable_sagemaker_reranker || var.sagemaker_reranker_endpoint_name != "" ? "conditional" : "disabled" },
    { name = "RAG_REMOTE_AUTH_MODE", value = "aws_iam" },
    { name = "RAG_ASYNC_INDEXING", value = "true" },
    { name = "RAG_CACHE_ENABLED", value = "true" },
    { name = "ENABLE_OTEL", value = "true" },
    { name = "OTEL_SERVICE_NAME", value = "incidentops-core" },
    { name = "MCP_ENABLED", value = "true" },
  ]

  runtime_secrets = [
    { name = "DATABASE_URL", valueFrom = "${aws_secretsmanager_secret.runtime.arn}:database_url::" },
    { name = "REDIS_URL", valueFrom = "${aws_secretsmanager_secret.runtime.arn}:redis_url::" },
    { name = "JWT_SECRET", valueFrom = "${aws_secretsmanager_secret.runtime.arn}:jwt_secret::" },
    { name = "QDRANT_API_KEY", valueFrom = "${aws_secretsmanager_secret.runtime.arn}:qdrant_api_key::" },
  ]

  reranker_endpoint_name     = var.enable_sagemaker_reranker ? aws_sagemaker_endpoint.reranker[0].name : var.sagemaker_reranker_endpoint_name
  reranker_iam_endpoint_name = local.reranker_endpoint_name != "" ? local.reranker_endpoint_name : "${local.name}-reranker-disabled"
}
