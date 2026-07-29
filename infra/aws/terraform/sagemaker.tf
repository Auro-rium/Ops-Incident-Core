resource "aws_iam_role" "sagemaker" {
  count = var.enable_sagemaker_reranker ? 1 : 0
  name  = "${local.name}-sagemaker-reranker"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "sagemaker.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "sagemaker" {
  count = var.enable_sagemaker_reranker ? 1 : 0
  name  = "runtime"
  role  = aws_iam_role.sagemaker[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:GetDownloadUrlForLayer"
        ]
        Resource = aws_ecr_repository.reranker.arn
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:${data.aws_partition.current.partition}:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/sagemaker/*"
      },
      {
        Effect = "Allow"
        Action = [
          "ec2:CreateNetworkInterface",
          "ec2:CreateNetworkInterfacePermission",
          "ec2:DeleteNetworkInterface",
          "ec2:DescribeNetworkInterfaces",
          "ec2:DescribeVpcs",
          "ec2:DescribeDhcpOptions",
          "ec2:DescribeSubnets",
          "ec2:DescribeSecurityGroups"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_sagemaker_model" "reranker" {
  count              = var.enable_sagemaker_reranker ? 1 : 0
  name               = "${local.name}-reranker"
  execution_role_arn = aws_iam_role.sagemaker[0].arn

  primary_container {
    image = "${aws_ecr_repository.reranker.repository_url}:${var.reranker_image_tag}"
    environment = {
      MODEL_ID       = var.reranker_model_id
      MODEL_REVISION = var.reranker_model_revision
      MODEL_DIR      = "/opt/incidentops/model"
    }
  }

  vpc_config {
    security_group_ids = [aws_security_group.sagemaker.id]
    subnets            = [for subnet in aws_subnet.private : subnet.id]
  }
}

resource "aws_sagemaker_endpoint_configuration" "reranker" {
  count = var.enable_sagemaker_reranker ? 1 : 0
  name  = "${local.name}-reranker"

  production_variants {
    variant_name           = "primary"
    model_name             = aws_sagemaker_model.reranker[0].name
    initial_instance_count = 1
    instance_type          = var.sagemaker_instance_type
  }
}

resource "aws_sagemaker_endpoint" "reranker" {
  count                = var.enable_sagemaker_reranker ? 1 : 0
  name                 = "${local.name}-reranker"
  endpoint_config_name = aws_sagemaker_endpoint_configuration.reranker[0].name
}
