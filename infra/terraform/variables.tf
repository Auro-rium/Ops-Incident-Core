variable "aws_region" {
  description = "AWS region for the IncidentOps Core deployment."
  type        = string
}

variable "project_name" {
  description = "Short name used in AWS resource names."
  type        = string
  default     = "incidentops-core"
}

variable "environment" {
  description = "Deployment environment name."
  type        = string
  default     = "production"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC."
  type        = string
  default     = "10.42.0.0/16"
}

variable "az_count" {
  description = "Number of availability zones to use."
  type        = number
  default     = 2
}

variable "enable_nat_gateway" {
  description = "Create a NAT gateway so private ECS tasks can reach ECR, CloudWatch, and external APIs."
  type        = bool
  default     = true
}

variable "container_port" {
  description = "API container port."
  type        = number
  default     = 8000
}

variable "image_tag" {
  description = "Initial Docker image tag for task definitions. CI/CD registers new revisions with commit tags."
  type        = string
  default     = "latest"
}

variable "image_uri" {
  description = "Optional full image URI. Defaults to the ECR repository created by this module."
  type        = string
  default     = ""
}

variable "api_desired_count" {
  description = "Desired number of API tasks."
  type        = number
  default     = 2
}

variable "worker_desired_count" {
  description = "Desired number of worker tasks."
  type        = number
  default     = 1
}

variable "api_cpu" {
  description = "API task CPU units."
  type        = number
  default     = 512
}

variable "api_memory" {
  description = "API task memory MiB."
  type        = number
  default     = 1024
}

variable "worker_cpu" {
  description = "Worker task CPU units."
  type        = number
  default     = 512
}

variable "worker_memory" {
  description = "Worker task memory MiB."
  type        = number
  default     = 1024
}

variable "db_name" {
  description = "RDS database name."
  type        = string
  default     = "incidentops"
}

variable "db_username" {
  description = "RDS master username."
  type        = string
  default     = "incidentops"
}

variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage" {
  description = "RDS allocated storage in GiB."
  type        = number
  default     = 20
}

variable "db_engine_version" {
  description = "RDS PostgreSQL engine version with pgvector extension support."
  type        = string
  default     = "16.3"
}

variable "db_backup_retention_days" {
  description = "RDS backup retention period."
  type        = number
  default     = 7
}

variable "db_multi_az" {
  description = "Enable Multi-AZ for RDS."
  type        = bool
  default     = false
}

variable "db_deletion_protection" {
  description = "Enable RDS deletion protection."
  type        = bool
  default     = true
}

variable "redis_node_type" {
  description = "ElastiCache Redis node type."
  type        = string
  default     = "cache.t4g.micro"
}

variable "redis_node_count" {
  description = "Number of Redis cache nodes."
  type        = number
  default     = 1
}

variable "redis_engine_version" {
  description = "ElastiCache Redis engine version."
  type        = string
  default     = "7.1"
}

variable "redis_transit_encryption_enabled" {
  description = "Use rediss:// for the generated REDIS_URL when transit encryption is enabled."
  type        = bool
  default     = false
}

variable "certificate_arn" {
  description = "Optional ACM certificate ARN. If set, ALB serves HTTPS and redirects HTTP to HTTPS."
  type        = string
  default     = ""
}

variable "cors_origins" {
  description = "Comma-separated allowed CORS origins for production."
  type        = string
}

variable "bootstrap_admin_email" {
  description = "Initial admin email stored in Secrets Manager for bootstrap jobs."
  type        = string
}

variable "bootstrap_admin_password" {
  description = "Initial admin password stored in Secrets Manager for bootstrap jobs. Use a strong value."
  type        = string
  sensitive   = true
}

variable "openai_api_key" {
  description = "Optional OpenAI-compatible API key. Leave blank for evidence-only mode."
  type        = string
  sensitive   = true
  default     = ""
}

variable "enable_otel" {
  description = "Enable OpenTelemetry hooks in the application."
  type        = bool
  default     = false
}

variable "otel_exporter_otlp_endpoint" {
  description = "Optional OTLP endpoint."
  type        = string
  default     = ""
}

variable "log_retention_days" {
  description = "CloudWatch log retention in days."
  type        = number
  default     = 30
}

variable "ecr_force_delete" {
  description = "Allow Terraform destroy to delete the ECR repository even when it contains images."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Additional tags."
  type        = map(string)
  default     = {}
}
