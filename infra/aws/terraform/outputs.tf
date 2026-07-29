output "public_url" {
  value = "${var.certificate_arn == "" ? "http" : "https"}://${aws_lb.main.dns_name}"
}

output "alb_dns_name" { value = aws_lb.main.dns_name }
output "ecs_cluster_name" { value = aws_ecs_cluster.main.name }
output "private_subnet_ids" { value = local.private_subnet_ids }
output "ecs_security_group_id" { value = aws_security_group.ecs.id }
output "runtime_secret_arn" { value = aws_secretsmanager_secret.runtime.arn }
output "qdrant_private_name" { value = "qdrant.${aws_service_discovery_private_dns_namespace.internal.name}" }
output "qdrant_instance_id" { value = aws_instance.qdrant.id }
output "backup_vault_name" { value = aws_backup_vault.qdrant.name }
output "backup_role_arn" { value = aws_iam_role.backup.arn }
output "qdrant_instance_arn" { value = aws_instance.qdrant.arn }
output "mcp_private_url" { value = "http://mcp.${aws_service_discovery_private_dns_namespace.internal.name}:8080/mcp" }
output "core_ecr_repository_url" { value = aws_ecr_repository.core.repository_url }
output "frontend_ecr_repository_url" { value = aws_ecr_repository.frontend.repository_url }
output "reranker_ecr_repository_url" { value = aws_ecr_repository.reranker.repository_url }
output "api_service_name" { value = aws_ecs_service.api.name }
output "worker_service_name" { value = aws_ecs_service.worker.name }
output "frontend_service_name" { value = aws_ecs_service.frontend.name }
output "collector_service_name" { value = aws_ecs_service.collector.name }
output "mcp_service_name" { value = aws_ecs_service.mcp.name }
output "api_task_definition_arn" { value = aws_ecs_task_definition.api.arn }
output "worker_task_definition_arn" { value = aws_ecs_task_definition.worker.arn }
output "frontend_task_definition_arn" { value = aws_ecs_task_definition.frontend.arn }
output "collector_task_definition_arn" { value = aws_ecs_task_definition.collector.arn }
output "mcp_task_definition_arn" { value = aws_ecs_task_definition.mcp.arn }
output "migration_task_definition_arn" { value = aws_ecs_task_definition.migration.arn }
output "bootstrap_task_definition_arn" { value = aws_ecs_task_definition.bootstrap.arn }
output "sagemaker_reranker_endpoint_name" { value = local.reranker_endpoint_name }

output "desired_counts" {
  value = {
    api       = var.api_desired_count
    worker    = var.worker_desired_count
    frontend  = var.frontend_desired_count
    collector = var.collector_desired_count
    mcp       = var.mcp_desired_count
  }
}
