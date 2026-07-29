resource "random_password" "database" {
  length  = 40
  special = false
}

resource "random_password" "redis" {
  length  = 48
  special = false
}

resource "random_password" "jwt" {
  length  = 64
  special = false
}

resource "random_password" "qdrant" {
  length  = 48
  special = false
}

resource "random_password" "bootstrap" {
  length  = 32
  special = false
}

resource "random_id" "final_snapshot" {
  byte_length = 4
}

resource "aws_db_subnet_group" "main" {
  name       = "${local.name}-database"
  subnet_ids = [for subnet in aws_subnet.private : subnet.id]
}

resource "aws_db_instance" "main" {
  identifier                            = "${local.name}-postgres"
  engine                                = "postgres"
  engine_version                        = "16"
  instance_class                        = var.database_instance_class
  allocated_storage                     = var.database_allocated_storage_gb
  max_allocated_storage                 = var.database_max_storage_gb
  storage_type                          = "gp3"
  storage_encrypted                     = true
  db_name                               = var.database_name
  username                              = var.database_username
  password                              = random_password.database.result
  port                                  = 5432
  db_subnet_group_name                  = aws_db_subnet_group.main.name
  vpc_security_group_ids                = [aws_security_group.database.id]
  publicly_accessible                   = false
  multi_az                              = var.database_multi_az
  backup_retention_period               = 7
  backup_window                         = "03:00-04:00"
  maintenance_window                    = "sun:04:00-sun:05:00"
  auto_minor_version_upgrade            = true
  deletion_protection                   = var.database_deletion_protection
  skip_final_snapshot                   = var.database_skip_final_snapshot
  final_snapshot_identifier             = var.database_skip_final_snapshot ? null : "${local.name}-final-${random_id.final_snapshot.hex}"
  performance_insights_enabled          = true
  performance_insights_retention_period = 7
  apply_immediately                     = false
  copy_tags_to_snapshot                 = true
}

resource "aws_elasticache_subnet_group" "main" {
  name       = "${local.name}-redis"
  subnet_ids = [for subnet in aws_subnet.private : subnet.id]
}

resource "aws_elasticache_replication_group" "main" {
  replication_group_id       = "${local.name}-redis"
  description                = "IncidentOps queue, rate limit, and retrieval cache"
  engine                     = "redis"
  node_type                  = var.redis_node_type
  port                       = 6379
  num_cache_clusters         = var.redis_num_cache_clusters
  subnet_group_name          = aws_elasticache_subnet_group.main.name
  security_group_ids         = [aws_security_group.redis.id]
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  auth_token                 = random_password.redis.result
  auth_token_update_strategy = "SET"
  automatic_failover_enabled = var.redis_num_cache_clusters > 1
  multi_az_enabled           = var.redis_num_cache_clusters > 1
  snapshot_retention_limit   = 3
  apply_immediately          = false
}

resource "aws_secretsmanager_secret" "runtime" {
  name                    = "${local.name}/runtime"
  description             = "IncidentOps runtime secrets injected into ECS and Qdrant"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "runtime" {
  secret_id = aws_secretsmanager_secret.runtime.id
  secret_string = jsonencode({
    database_url             = "postgresql+asyncpg://${var.database_username}:${random_password.database.result}@${aws_db_instance.main.address}:5432/${var.database_name}?ssl=require"
    redis_url                = "rediss://:${random_password.redis.result}@${aws_elasticache_replication_group.main.primary_endpoint_address}:6379/0"
    jwt_secret               = random_password.jwt.result
    qdrant_api_key           = random_password.qdrant.result
    bootstrap_admin_email    = var.bootstrap_admin_email
    bootstrap_admin_password = var.bootstrap_admin_password != "" ? var.bootstrap_admin_password : random_password.bootstrap.result
    collector_access_token   = var.collector_access_token != "" ? var.collector_access_token : "disabled"
    mcp_access_token         = var.mcp_access_token != "" ? var.mcp_access_token : "disabled"
  })
}

resource "aws_ecr_repository" "core" {
  name                 = "${local.name}/core"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }
}

resource "aws_ecr_repository" "frontend" {
  name                 = "${local.name}/frontend"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }
}

resource "aws_ecr_repository" "reranker" {
  name                 = "${local.name}/reranker"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }
}

resource "aws_cloudwatch_log_group" "services" {
  for_each = toset(["api", "worker", "frontend", "collector", "mcp", "migration", "bootstrap", "qdrant"])

  name              = "/incidentops/${local.name}/${each.key}"
  retention_in_days = 30
}

resource "aws_budgets_budget" "monthly" {
  count = var.budget_alert_email == "" ? 0 : 1

  name         = "${local.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = {
      actual_50    = [50, "ACTUAL"]
      actual_80    = [80, "ACTUAL"]
      forecast_100 = [100, "FORECASTED"]
    }
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value[0]
      threshold_type             = "PERCENTAGE"
      notification_type          = notification.value[1]
      subscriber_email_addresses = [var.budget_alert_email]
    }
  }
}
