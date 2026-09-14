terraform {
  required_version = ">= 1.6"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}
variable "account_id" { type = string }
variable "subnet_id" { type = string }
variable "vpc_id" { type = string }
variable "ami" { type = string }
variable "region" { default = "us-east-1" }
variable "name" { default = "fintech-rag-portfolio" }
provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
}
locals {
  bucket  = "${var.name}-${var.account_id}"
  oidc    = "arn:aws:iam::${var.account_id}:oidc-provider/token.actions.githubusercontent.com"
  subject = "repo:ezhilan03@52958983/fintech-rag@1311275322:environment:rag-release"
}
resource "aws_ecr_repository" "app" {
  name                 = var.name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false
  image_scanning_configuration { scan_on_push = true }
}
resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy     = jsonencode({ rules = [{ rulePriority = 1, description = "Expire untagged build layers after seven days", selection = { tagStatus = "untagged", countType = "sinceImagePushed", countUnit = "days", countNumber = 7 }, action = { type = "expire" } }] })
}
resource "aws_s3_bucket" "artifacts" {
  bucket        = local.bucket
  force_destroy = false
}
resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_bucket_policy" "tls" {
  bucket = aws_s3_bucket.artifacts.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Deny", Principal = "*", Action = "s3:*", Resource = [aws_s3_bucket.artifacts.arn, "${aws_s3_bucket.artifacts.arn}/*"], Condition = { Bool = { "aws:SecureTransport" = "false" } } }] })
}
resource "aws_cloudwatch_log_group" "app" {
  name              = "/portfolio/${var.name}"
  retention_in_days = 7
}
resource "aws_iam_role" "runtime" {
  name               = "${var.name}-runtime"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "ec2.amazonaws.com" } }] })
}
resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.runtime.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}
resource "aws_iam_role_policy" "runtime" {
  role = aws_iam_role.runtime.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["ecr:GetAuthorizationToken", "logs:DescribeLogGroups"], Resource = "*" },
    { Effect = "Allow", Action = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"], Resource = aws_ecr_repository.app.arn },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = ["${aws_s3_bucket.artifacts.arn}/releases/*"] },
    { Effect = "Allow", Action = ["s3:PutObject"], Resource = ["${aws_s3_bucket.artifacts.arn}/reports/*", "${aws_s3_bucket.artifacts.arn}/backups/*"] },
    { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = "arn:aws:ssm:${var.region}:${var.account_id}:parameter/${var.name}/anthropic-key" },
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"], Resource = "${aws_cloudwatch_log_group.app.arn}:*" },
    { Effect = "Allow", Action = ["cloudwatch:PutMetricData"], Resource = "*", Condition = { StringEquals = { "cloudwatch:namespace" = "Portfolio/FintechRAG" } } }
  ] })
}
resource "aws_iam_instance_profile" "runtime" { role = aws_iam_role.runtime.name }
resource "aws_security_group" "app" {
  name_prefix = "${var.name}-"
  vpc_id      = var.vpc_id
  ingress     = []
  egress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
resource "aws_instance" "app" {
  ami                                  = var.ami
  instance_type                        = "t4g.small"
  subnet_id                            = var.subnet_id
  vpc_security_group_ids               = [aws_security_group.app.id]
  iam_instance_profile                 = aws_iam_instance_profile.runtime.name
  associate_public_ip_address          = true
  instance_initiated_shutdown_behavior = "stop"
  credit_specification { cpu_credits = "standard" }
  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  root_block_device {
    volume_type           = "gp3"
    volume_size           = 12
    encrypted             = true
    delete_on_termination = false
  }
  user_data                   = <<-SCRIPT
    #!/bin/bash
    set -eu
    cat >/etc/systemd/system/rag-stop.service <<'UNIT'
    [Service]
    Type=oneshot
    ExecStart=/usr/sbin/shutdown -h now
    UNIT
    cat >/etc/systemd/system/rag-stop.timer <<'UNIT'
    [Timer]
    OnBootSec=60min
    Unit=rag-stop.service
    [Install]
    WantedBy=timers.target
    UNIT
    systemctl daemon-reload
    systemctl enable --now rag-stop.timer
    dnf install -y docker
    systemctl enable --now docker
  SCRIPT
  user_data_replace_on_change = true
  tags                        = { Name = "${var.name}-demo" }
  depends_on                  = [aws_iam_role_policy.runtime, aws_iam_role_policy_attachment.ssm]
}
resource "aws_ssm_document" "demo" {
  name          = "${var.name}-run"
  document_type = "Command"
  content = jsonencode({ schemaVersion = "2.2", description = "Run and verify a bounded synthetic RAG release", parameters = { Revision = { type = "String", allowedPattern = "^[0-9a-f]{40}$" } }, mainSteps = [{
    action = "aws:runShellScript", name = "rag", inputs = { timeoutSeconds = "1500", runCommand = [
      "set -eu", "cloud-init status --wait", "install -d -m 700 /var/lib/fintech-rag",
      "echo '${base64encode(file("${path.module}/../../scripts/cloud_host.py"))}' | base64 -d >/var/lib/fintech-rag/run.py",
      "python3 /var/lib/fintech-rag/run.py '{{ Revision }}' '${aws_s3_bucket.artifacts.id}' '${aws_ecr_repository.app.repository_url}' '${var.region}' '/${var.name}/anthropic-key' '${aws_cloudwatch_log_group.app.name}'"
    ] }
  }] })
}
resource "aws_iam_role" "release" {
  name               = "${var.name}-release"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{ Effect = "Allow", Action = "sts:AssumeRoleWithWebIdentity", Principal = { Federated = local.oidc }, Condition = { StringEquals = { "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com", "token.actions.githubusercontent.com:sub" = local.subject } } }] })
}
resource "aws_iam_role_policy" "release" {
  role = aws_iam_role.release.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
    { Effect = "Allow", Action = ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:DescribeImages"], Resource = aws_ecr_repository.app.arn },
    { Effect = "Allow", Action = ["s3:PutObject"], Resource = "${aws_s3_bucket.artifacts.arn}/releases/*" },
    { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.artifacts.arn}/reports/*" },
    { Effect = "Allow", Action = ["ec2:StartInstances", "ec2:StopInstances"], Resource = aws_instance.app.arn },
    { Effect = "Allow", Action = ["ec2:DescribeInstances", "ssm:DescribeInstanceInformation", "ssm:GetCommandInvocation"], Resource = "*" },
    { Effect = "Allow", Action = ["ssm:SendCommand"], Resource = [aws_instance.app.arn, aws_ssm_document.demo.arn] }
  ] })
}
output "bucket" { value = aws_s3_bucket.artifacts.id }
output "repository" { value = aws_ecr_repository.app.repository_url }
output "instance" { value = aws_instance.app.id }
output "role" { value = aws_iam_role.release.arn }
output "document" { value = aws_ssm_document.demo.name }
