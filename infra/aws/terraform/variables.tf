variable "aws_region" {
  description = "AWS region for the complete IncidentOps runtime."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Short lowercase resource prefix."
  type        = string
  default     = "incidentops"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,20}$", var.name_prefix))
    error_message = "name_prefix must be 3-21 lowercase alphanumeric or hyphen characters."
  }
}

variable "environment" {
  type    = string
  default = "production"

  validation {
    condition     = contains(["staging", "production"], var.environment)
    error_message = "environment must be staging or production."
  }
}

variable "vpc_cidr" {
  type    = string
  default = "10.42.0.0/16"
}

variable "availability_zone_count" {
  type    = number
  default = 2

  validation {
    condition     = var.availability_zone_count >= 2 && var.availability_zone_count <= 3
    error_message = "availability_zone_count must be 2 or 3."
  }
}

variable "enable_nat_gateway" {
  description = "Required for Collector Git egress and Qdrant image pulls unless equivalent private egress is supplied."
  type        = bool
  default     = true
}

variable "allowed_http_cidrs" {
  type    = list(string)
  default = ["0.0.0.0/0"]
}

variable "certificate_arn" {
  description = "Optional ACM certificate. When set, HTTP redirects to HTTPS."
  type        = string
  default     = ""
}

variable "cors_origins" {
  description = "Comma-separated exact browser origins. Wildcards are rejected by Core."
  type        = string
}

variable "core_image_tag" {
  type    = string
  default = "bootstrap"
}

variable "frontend_image_tag" {
  type    = string
  default = "bootstrap"
}

variable "reranker_image_tag" {
  type    = string
  default = "bootstrap"
}

variable "database_name" {
  type    = string
  default = "incidentops"
}

variable "database_username" {
  type    = string
  default = "incidentops"
}

variable "database_instance_class" {
  type    = string
  default = "db.t4g.medium"
}

variable "database_allocated_storage_gb" {
  type    = number
  default = 50
}

variable "database_max_storage_gb" {
  type    = number
  default = 200
}

variable "database_backup_retention_days" {
  description = "RDS automated backup retention. Restricted/free-plan accounts may cap this at one day."
  type        = number
  default     = 1

  validation {
    condition     = var.database_backup_retention_days >= 1 && var.database_backup_retention_days <= 35
    error_message = "database_backup_retention_days must be between 1 and 35."
  }
}

variable "database_multi_az" {
  type    = bool
  default = false
}

variable "database_deletion_protection" {
  type    = bool
  default = true
}

variable "database_skip_final_snapshot" {
  type    = bool
  default = false
}

variable "redis_node_type" {
  type    = string
  default = "cache.t4g.small"
}

variable "redis_num_cache_clusters" {
  type    = number
  default = 1
}

variable "qdrant_instance_type" {
  type    = string
  default = "t3.medium"
}

variable "qdrant_volume_size_gb" {
  type    = number
  default = 100
}

variable "qdrant_image" {
  type    = string
  default = "qdrant/qdrant:v1.13.2"
}

variable "qdrant_collection" {
  type    = string
  default = "incidentops_chunks"
}

variable "vector_index_version" {
  type    = string
  default = "aws-v1"
}

variable "bedrock_chat_model_id" {
  description = "Bedrock Converse-compatible model or inference-profile ID."
  type        = string
  default     = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
}

variable "bedrock_embedding_model_id" {
  type    = string
  default = "amazon.titan-embed-text-v2:0"
}

variable "embedding_dimension" {
  type    = number
  default = 1024

  validation {
    condition     = var.embedding_dimension == 1024
    error_message = "Phase 6 requires Titan Text Embeddings V2 at 1024 dimensions."
  }
}

variable "enable_sagemaker_reranker" {
  description = "Creates a billable always-on GPU endpoint when true."
  type        = bool
  default     = false
}

variable "sagemaker_reranker_endpoint_name" {
  description = "Existing endpoint name when Terraform is not creating one."
  type        = string
  default     = ""
}

variable "reranker_model_id" {
  type    = string
  default = "BAAI/bge-reranker-v2-m3"
}

variable "reranker_model_revision" {
  description = "Immutable Hugging Face commit used while building the reranker image."
  type        = string
  default     = ""
}

variable "sagemaker_instance_type" {
  type    = string
  default = "ml.g5.xlarge"
}

variable "api_cpu" {
  type    = number
  default = 1024
}

variable "api_memory" {
  type    = number
  default = 2048
}

variable "worker_cpu" {
  type    = number
  default = 1024
}

variable "worker_memory" {
  type    = number
  default = 2048
}

variable "frontend_cpu" {
  type    = number
  default = 512
}

variable "frontend_memory" {
  type    = number
  default = 1024
}

variable "api_desired_count" {
  type    = number
  default = 2
}

variable "worker_desired_count" {
  type    = number
  default = 1
}

variable "frontend_desired_count" {
  type    = number
  default = 2
}

variable "mcp_desired_count" {
  type    = number
  default = 0
}

variable "collector_desired_count" {
  type    = number
  default = 0
}

variable "collector_project_id" {
  type    = string
  default = ""
}

variable "collector_repo_url" {
  type    = string
  default = ""
}

variable "collector_source_name" {
  type    = string
  default = "production-repository"
}

variable "collector_access_token" {
  description = "Project-scoped Core access token for the Collector. Leave empty while collector_desired_count is zero."
  type        = string
  sensitive   = true
  default     = ""
}

variable "mcp_access_token" {
  description = "Project-scoped Core access token for MCP. Leave empty while mcp_desired_count is zero."
  type        = string
  sensitive   = true
  default     = ""
}

variable "bootstrap_admin_email" {
  type    = string
  default = "admin@incidentops.local"
}

variable "bootstrap_admin_password" {
  description = "Optional bootstrap password. Terraform generates one when this is empty."
  type        = string
  sensitive   = true
  default     = ""
}

variable "monthly_budget_usd" {
  type    = number
  default = 300
}

variable "budget_alert_email" {
  type    = string
  default = ""
}
