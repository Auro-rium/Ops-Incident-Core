data "aws_ami" "amazon_linux" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-x86_64"]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
}

resource "aws_iam_role" "qdrant" {
  name = "${local.name}-qdrant"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "qdrant_ssm" {
  role       = aws_iam_role.qdrant.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "qdrant_runtime" {
  name = "runtime"
  role = aws_iam_role.qdrant.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = aws_secretsmanager_secret.runtime.arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
        Resource = "${aws_cloudwatch_log_group.services["qdrant"].arn}:*"
      }
    ]
  })
}

resource "aws_iam_instance_profile" "qdrant" {
  name = "${local.name}-qdrant"
  role = aws_iam_role.qdrant.name
}

resource "aws_instance" "qdrant" {
  ami                         = data.aws_ami.amazon_linux.id
  instance_type               = var.qdrant_instance_type
  subnet_id                   = values(aws_subnet.private)[0].id
  vpc_security_group_ids      = [aws_security_group.qdrant.id]
  iam_instance_profile        = aws_iam_instance_profile.qdrant.name
  associate_public_ip_address = false
  monitoring                  = true

  root_block_device {
    encrypted   = true
    volume_type = "gp3"
    volume_size = 20
  }

  ebs_block_device {
    device_name           = "/dev/xvdb"
    encrypted             = true
    volume_type           = "gp3"
    volume_size           = var.qdrant_volume_size_gb
    delete_on_termination = false
  }

  volume_tags = {
    Name   = "${local.name}-qdrant-data"
    Backup = "incidentops"
  }

  user_data_base64 = base64encode(templatefile("${path.module}/qdrant-user-data.sh.tftpl", {
    aws_region        = var.aws_region
    qdrant_image      = var.qdrant_image
    runtime_secret_id = aws_secretsmanager_secret.runtime.id
    log_group_name    = aws_cloudwatch_log_group.services["qdrant"].name
  }))

  user_data_replace_on_change = true

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  tags = {
    Name   = "${local.name}-qdrant"
    Backup = "incidentops"
  }

  depends_on = [aws_secretsmanager_secret_version.runtime]
}

resource "aws_service_discovery_service" "qdrant" {
  name = "qdrant"

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

resource "aws_service_discovery_instance" "qdrant" {
  instance_id = aws_instance.qdrant.id
  service_id  = aws_service_discovery_service.qdrant.id
  attributes = {
    AWS_INSTANCE_IPV4 = aws_instance.qdrant.private_ip
    AWS_INSTANCE_PORT = "6333"
  }
}

resource "aws_iam_role" "backup" {
  name = "${local.name}-backup"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "backup.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "backup" {
  role       = aws_iam_role.backup.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup"
}

resource "aws_backup_vault" "qdrant" {
  name = "${local.name}-qdrant"
}

resource "aws_backup_plan" "qdrant" {
  name = "${local.name}-qdrant"
  rule {
    rule_name         = "daily"
    target_vault_name = aws_backup_vault.qdrant.name
    schedule          = "cron(0 5 * * ? *)"
    lifecycle { delete_after = 14 }
  }
}

resource "aws_backup_selection" "qdrant" {
  name         = "${local.name}-qdrant"
  iam_role_arn = aws_iam_role.backup.arn
  plan_id      = aws_backup_plan.qdrant.id
  resources    = [aws_instance.qdrant.arn]
}
