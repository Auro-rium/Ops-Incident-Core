resource "aws_ecs_cluster" "main" {
  name = local.name
  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_iam_role" "ecs_execution" {
  name = "${local.name}-ecs-execution"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ecs_execution" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "ecs_execution_secrets" {
  name = "runtime-secrets"
  role = aws_iam_role.ecs_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["secretsmanager:GetSecretValue"]
      Resource = aws_secretsmanager_secret.runtime.arn
    }]
  })
}

resource "aws_iam_role" "ecs_task" {
  name = "${local.name}-ecs-task"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "ecs_task" {
  name = "model-runtime"
  role = aws_iam_role.ecs_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["sagemaker:InvokeEndpoint"]
        Resource = "arn:${data.aws_partition.current.partition}:sagemaker:${var.aws_region}:${data.aws_caller_identity.current.account_id}:endpoint/${local.reranker_iam_endpoint_name}"
      },
      {
        Effect = "Allow"
        Action = [
          "ssmmessages:CreateControlChannel",
          "ssmmessages:CreateDataChannel",
          "ssmmessages:OpenControlChannel",
          "ssmmessages:OpenDataChannel"
        ]
        Resource = "*"
      }
    ]
  })
}

locals {
  core_image     = "${aws_ecr_repository.core.repository_url}:${var.core_image_tag}"
  frontend_image = "${aws_ecr_repository.frontend.repository_url}:${var.frontend_image_tag}"

  awslogs_options = {
    awslogs-region        = var.aws_region
    awslogs-stream-prefix = "ecs"
  }
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.api_cpu)
  memory                   = tostring(var.api_memory)
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([{
    name         = "api"
    image        = local.core_image
    essential    = true
    command      = ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
    environment  = local.common_environment
    secrets      = local.runtime_secrets
    portMappings = [{ containerPort = 8000, hostPort = 8000, protocol = "tcp" }]
    logConfiguration = {
      logDriver = "awslogs"
      options   = merge(local.awslogs_options, { awslogs-group = aws_cloudwatch_log_group.services["api"].name })
    }
  }])
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.worker_cpu)
  memory                   = tostring(var.worker_memory)
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([{
    name        = "worker"
    image       = local.core_image
    essential   = true
    command     = ["python", "-m", "incidentops.worker"]
    environment = local.common_environment
    secrets     = local.runtime_secrets
    logConfiguration = {
      logDriver = "awslogs"
      options   = merge(local.awslogs_options, { awslogs-group = aws_cloudwatch_log_group.services["worker"].name })
    }
  }])
}

resource "aws_ecs_task_definition" "frontend" {
  family                   = "${local.name}-frontend"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.frontend_cpu)
  memory                   = tostring(var.frontend_memory)
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([{
    name      = "frontend"
    image     = local.frontend_image
    essential = true
    environment = [
      { name = "NODE_ENV", value = "production" },
      { name = "PORT", value = "3000" },
      { name = "HOSTNAME", value = "0.0.0.0" },
      { name = "CORE_API_BASE_URL", value = "http://api.${aws_service_discovery_private_dns_namespace.internal.name}:8000" },
    ]
    portMappings = [{ containerPort = 3000, hostPort = 3000, protocol = "tcp" }]
    logConfiguration = {
      logDriver = "awslogs"
      options   = merge(local.awslogs_options, { awslogs-group = aws_cloudwatch_log_group.services["frontend"].name })
    }
  }])
}

resource "aws_ecs_task_definition" "collector" {
  family                   = "${local.name}-collector"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "1024"
  memory                   = "2048"
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([{
    name      = "collector"
    image     = local.core_image
    essential = true
    command   = ["python", "-m", "incidentops.collector", "daemon"]
    environment = concat(local.common_environment, [
      { name = "INCIDENTOPS_API_URL", value = "http://api.${aws_service_discovery_private_dns_namespace.internal.name}:8000" },
      { name = "INCIDENTOPS_PROJECT_ID", value = var.collector_project_id },
      { name = "COLLECTOR_REPO_URL", value = var.collector_repo_url },
      { name = "SOURCE_NAME", value = var.collector_source_name },
      { name = "COLLECTOR_ENVIRONMENT", value = var.environment },
    ])
    secrets = [{
      name      = "INCIDENTOPS_TOKEN"
      valueFrom = "${aws_secretsmanager_secret.runtime.arn}:collector_access_token::"
    }]
    logConfiguration = {
      logDriver = "awslogs"
      options   = merge(local.awslogs_options, { awslogs-group = aws_cloudwatch_log_group.services["collector"].name })
    }
  }])
}

resource "aws_ecs_task_definition" "mcp" {
  family                   = "${local.name}-mcp"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([{
    name      = "mcp"
    image     = local.core_image
    essential = true
    command   = ["python", "-m", "incidentops.mcp.server"]
    environment = concat(local.common_environment, [
      { name = "MCP_CORE_API_URL", value = "http://api.${aws_service_discovery_private_dns_namespace.internal.name}:8000" },
      { name = "MCP_TRANSPORT", value = "streamable-http" },
      { name = "MCP_HOST", value = "0.0.0.0" },
      { name = "MCP_PORT", value = "8080" },
    ])
    secrets = [{
      name      = "MCP_TOKEN"
      valueFrom = "${aws_secretsmanager_secret.runtime.arn}:mcp_access_token::"
    }]
    portMappings = [{ containerPort = 8080, hostPort = 8080, protocol = "tcp" }]
    logConfiguration = {
      logDriver = "awslogs"
      options   = merge(local.awslogs_options, { awslogs-group = aws_cloudwatch_log_group.services["mcp"].name })
    }
  }])
}

resource "aws_ecs_task_definition" "migration" {
  family                   = "${local.name}-migration"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "1024"
  memory                   = "2048"
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([{
    name        = "migration"
    image       = local.core_image
    essential   = true
    command     = ["sh", "-c", "alembic upgrade head && python scripts/check_migrations.py"]
    environment = local.common_environment
    secrets     = local.runtime_secrets
    logConfiguration = {
      logDriver = "awslogs"
      options   = merge(local.awslogs_options, { awslogs-group = aws_cloudwatch_log_group.services["migration"].name })
    }
  }])
}

resource "aws_ecs_task_definition" "bootstrap" {
  family                   = "${local.name}-bootstrap"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([{
    name        = "bootstrap"
    image       = local.core_image
    essential   = true
    command     = ["python", "-m", "incidentops.security.bootstrap_admin"]
    environment = local.common_environment
    secrets = concat(local.runtime_secrets, [
      { name = "BOOTSTRAP_ADMIN_EMAIL", valueFrom = "${aws_secretsmanager_secret.runtime.arn}:bootstrap_admin_email::" },
      { name = "BOOTSTRAP_ADMIN_PASSWORD", valueFrom = "${aws_secretsmanager_secret.runtime.arn}:bootstrap_admin_password::" },
    ])
    logConfiguration = {
      logDriver = "awslogs"
      options   = merge(local.awslogs_options, { awslogs-group = aws_cloudwatch_log_group.services["bootstrap"].name })
    }
  }])
}

resource "aws_lb" "main" {
  name                       = substr("${local.name}-public", 0, 32)
  internal                   = false
  load_balancer_type         = "application"
  security_groups            = [aws_security_group.alb.id]
  subnets                    = [for subnet in aws_subnet.public : subnet.id]
  drop_invalid_header_fields = true
}

resource "aws_lb_target_group" "frontend" {
  name        = substr("${local.name}-frontend", 0, 32)
  port        = 3000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.main.id

  health_check {
    enabled             = true
    path                = "/"
    healthy_threshold   = 2
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 30
    matcher             = "200-399"
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  dynamic "default_action" {
    for_each = var.certificate_arn == "" ? [1] : []
    content {
      type             = "forward"
      target_group_arn = aws_lb_target_group.frontend.arn
    }
  }

  dynamic "default_action" {
    for_each = var.certificate_arn == "" ? [] : [1]
    content {
      type = "redirect"
      redirect {
        port        = "443"
        protocol    = "HTTPS"
        status_code = "HTTP_301"
      }
    }
  }
}

resource "aws_lb_listener" "https" {
  count = var.certificate_arn == "" ? 0 : 1

  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = var.certificate_arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.frontend.arn
  }
}

resource "aws_service_discovery_service" "api" {
  name = "api"
  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.internal.id
    dns_records {
      ttl  = 10
      type = "A"
    }
    routing_policy = "MULTIVALUE"
  }
  health_check_custom_config {}

  lifecycle {
    ignore_changes = [health_check_custom_config]
  }
}

resource "aws_service_discovery_service" "mcp" {
  name = "mcp"
  dns_config {
    namespace_id = aws_service_discovery_private_dns_namespace.internal.id
    dns_records {
      ttl  = 10
      type = "A"
    }
    routing_policy = "MULTIVALUE"
  }
  health_check_custom_config {}

  lifecycle {
    ignore_changes = [health_check_custom_config]
  }
}

locals {
  private_subnet_ids = [for subnet in aws_subnet.private : subnet.id]
}

resource "aws_ecs_service" "api" {
  name                   = "${local.name}-api"
  cluster                = aws_ecs_cluster.main.id
  task_definition        = aws_ecs_task_definition.api.arn
  desired_count          = 0
  launch_type            = "FARGATE"
  enable_execute_command = true
  network_configuration {
    subnets          = local.private_subnet_ids
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = false
  }
  service_registries { registry_arn = aws_service_discovery_service.api.arn }
  lifecycle { ignore_changes = [desired_count, task_definition] }
}

resource "aws_ecs_service" "worker" {
  name                   = "${local.name}-worker"
  cluster                = aws_ecs_cluster.main.id
  task_definition        = aws_ecs_task_definition.worker.arn
  desired_count          = 0
  launch_type            = "FARGATE"
  enable_execute_command = true
  network_configuration {
    subnets          = local.private_subnet_ids
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = false
  }
  lifecycle { ignore_changes = [desired_count, task_definition] }
}

resource "aws_ecs_service" "frontend" {
  name                              = "${local.name}-frontend"
  cluster                           = aws_ecs_cluster.main.id
  task_definition                   = aws_ecs_task_definition.frontend.arn
  desired_count                     = 0
  launch_type                       = "FARGATE"
  enable_execute_command            = true
  health_check_grace_period_seconds = 60
  network_configuration {
    subnets          = local.private_subnet_ids
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = false
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.frontend.arn
    container_name   = "frontend"
    container_port   = 3000
  }
  lifecycle { ignore_changes = [desired_count, task_definition] }
  depends_on = [aws_lb_listener.http]
}

resource "aws_ecs_service" "collector" {
  name            = "${local.name}-collector"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.collector.arn
  desired_count   = 0
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = local.private_subnet_ids
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = false
  }
  lifecycle { ignore_changes = [desired_count, task_definition] }
}

resource "aws_ecs_service" "mcp" {
  name            = "${local.name}-mcp"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.mcp.arn
  desired_count   = 0
  launch_type     = "FARGATE"
  network_configuration {
    subnets          = local.private_subnet_ids
    security_groups  = [aws_security_group.ecs.id]
    assign_public_ip = false
  }
  service_registries { registry_arn = aws_service_discovery_service.mcp.arn }
  lifecycle { ignore_changes = [desired_count, task_definition] }
}
