output "api_url" {
  description = "HTTP(S) URL for the Core API."
  value       = var.certificate_arn == "" ? "http://${aws_lb.api.dns_name}" : "https://${aws_lb.api.dns_name}"
}

output "alb_dns_name" {
  description = "Application Load Balancer DNS name."
  value       = aws_lb.api.dns_name
}

output "ecr_repository_url" {
  description = "ECR repository URL for Core images."
  value       = aws_ecr_repository.core.repository_url
}

output "ecr_repository_name" {
  description = "ECR repository name for CI/CD."
  value       = aws_ecr_repository.core.name
}

output "ecs_cluster_name" {
  description = "ECS cluster name."
  value       = aws_ecs_cluster.this.name
}

output "ecs_api_service_name" {
  description = "ECS API service name."
  value       = aws_ecs_service.api.name
}

output "ecs_worker_service_name" {
  description = "ECS worker service name."
  value       = aws_ecs_service.worker.name
}

output "ecs_api_task_definition_family" {
  description = "API task definition family for CI/CD."
  value       = aws_ecs_task_definition.api.family
}

output "ecs_worker_task_definition_family" {
  description = "Worker task definition family for CI/CD."
  value       = aws_ecs_task_definition.worker.family
}

output "database_url_secret_arn" {
  description = "Secrets Manager ARN for DATABASE_URL."
  value       = aws_secretsmanager_secret.database_url.arn
}

output "redis_url_secret_arn" {
  description = "Secrets Manager ARN for REDIS_URL."
  value       = aws_secretsmanager_secret.redis_url.arn
}
