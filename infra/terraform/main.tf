# FlashHunter: инфраструктура продакшна.
#   * VPC с приватными подсетями: бот не имеет публичного IP;
#   * EC2 (горячий путь) в приватной подсети + NAT для выхода к RPC/релеям;
#   * RDS Postgres (транзакционные данные) и S3 (бэкапы, отчёты);
#   * Cloudflare для DNS и TLS-фронта панели.
# Принцип: минимум публично доступных ресурсов, время жизни ключей ограничено.

data "aws_availability_zones" "available" {
  state = "available"
}

# --------------------------------------------------------------------------- #
# Сеть
# --------------------------------------------------------------------------- #
resource "aws_vpc" "main" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = { Name = "flashhunter-vpc" }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.main.id
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 4, count.index)
  availability_zone = data.aws_availability_zones.available.names[count.index]

  tags = { Name = "flashhunter-private-${count.index}" }
}

resource "aws_subnet" "public" {
  count                   = 2
  vpc_id                  = aws_vpc.main.id
  cidr_block              = cidrsubnet(aws_vpc.main.cidr_block, 4, count.index + 8)
  availability_zone       = data.aws_availability_zones.available.names[count.index]
  map_public_ip_on_launch = false

  tags = { Name = "flashhunter-public-${count.index}" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "flashhunter-igw" }
}

# NAT обязателен: боту нужен исходящий доступ к RPC-провайдерам и MEV-релеям,
# при этом входящего трафика из интернета быть не должно.
resource "aws_eip" "nat" {
  domain = "vpc"
  tags   = { Name = "flashhunter-nat-eip" }
}

resource "aws_nat_gateway" "main" {
  allocation_id = aws_eip.nat.id
  subnet_id     = aws_subnet.public[0].id
  tags          = { Name = "flashhunter-nat" }
  depends_on    = [aws_internet_gateway.main]
}

resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.main.id
  }

  tags = { Name = "flashhunter-private-rt" }
}

resource "aws_route_table_association" "private" {
  count          = 2
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private.id
}

# --------------------------------------------------------------------------- #
# Безопасность
# --------------------------------------------------------------------------- #
resource "aws_security_group" "hotpath" {
  name        = "flashhunter-hotpath"
  description = "Горячий путь бота: только исходящий трафик + панель по HTTPS из разрешённых CIDR"
  vpc_id      = aws_vpc.main.id

  # Входящие: только панель (443) и SSH из доверенных сетей
  ingress {
    description = "Панель управления"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = var.allowed_admin_cidrs
  }

  ingress {
    description = "SSH только из доверенных сетей (ключи, без пароля)"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = var.allowed_admin_cidrs
  }

  # Исходящие: HTTPS (RPC, релеи, пакеты), RPC-порты, NTP
  egress {
    description = "HTTPS к RPC и релеям"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "Прямые RPC-эндпоинты"
    from_port   = 8545
    to_port     = 8546
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    description = "NTP: точное время важно для дедлайнов транзакций"
    from_port   = 123
    to_port     = 123
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "flashhunter-hotpath-sg" }
}

resource "aws_kms_key" "keys" {
  description             = "Шифрование приватных ключей keeper-кошелька и бэкапов"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  tags = { Name = "flashhunter-keys" }
}

resource "aws_kms_alias" "keys" {
  name          = "alias/flashhunter-keys"
  target_key_id = aws_kms_key.keys.key_id
}

# --------------------------------------------------------------------------- #
# Горячий путь: EC2
# --------------------------------------------------------------------------- #
data "aws_ami" "ubuntu" {
  most_recent = true

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }

  owners = ["099720109477"] # Canonical
}

resource "aws_instance" "hotpath" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.private[0].id
  vpc_security_group_ids = [aws_security_group.hotpath.id]
  key_name               = length(var.keeper_public_keys) > 0 ? aws_key_pair.hotpath[0].key_name : null

  # Диск: gp3 с повышенным IOPS — журнал сделок пишется постоянно
  root_block_device {
    volume_type           = "gp3"
    volume_size           = 100
    iops                  = 6000
    throughput            = 250
    encrypted             = true
    kms_key_id            = aws_kms_key.keys.arn
    delete_on_termination = true
  }

  # Защита от случайного удаления: терраформ-разрушение не должно сносить ноду с ключами
  disable_api_termination = true

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required" # IMDSv2 обязателен: защита от SSRF-кражи ролей
    http_put_response_hop_limit = 1
  }

  user_data = file("${path.module}/cloud-init.yaml")

  tags = { Name = "flashhunter-hotpath" }
}

resource "aws_key_pair" "hotpath" {
  count      = length(var.keeper_public_keys) > 0 ? 1 : 0
  key_name   = "flashhunter-hotpath"
  public_key = var.keeper_public_keys[0]
}

# Роль инстанса: чтение секретов из Secrets Manager, запись метрик в CloudWatch
resource "aws_iam_role" "hotpath" {
  name = "flashhunter-hotpath-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "hotpath" {
  name = "flashhunter-hotpath-policy"
  role = aws_iam_role.hotpath.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        Resource = [aws_secretsmanager_secret.runtime.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject", "s3:ListBucket"]
        Resource = [aws_s3_bucket.backups.arn, "${aws_s3_bucket.backups.arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = [aws_kms_key.keys.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["arn:aws:logs:${var.region}:*:log-group:/flashhunter/*"]
      }
    ]
  })
}

resource "aws_iam_instance_profile" "hotpath" {
  name = "flashhunter-hotpath-profile"
  role = aws_iam_role.hotpath.name
}

# --------------------------------------------------------------------------- #
# Секреты продакшна (RPC-ключи, JWT, авторизация релеев)
# --------------------------------------------------------------------------- #
resource "random_password" "jwt_secret" {
  length  = 48
  special = false
}

resource "aws_secretsmanager_secret" "runtime" {
  name        = "flashhunter/${var.environment}/runtime"
  description = "Секреты рантайма FlashHunter: JWT, RPC-ключи, авторизация релеев"
  kms_key_id  = aws_kms_key.keys.arn

  recovery_window_in_days = 30 # случайное удаление секрета можно отменить месяц
}

resource "aws_secretsmanager_secret_version" "runtime" {
  secret_id = aws_secretsmanager_secret.runtime.id

  # Значения приходят из CI (TF_VAR_*) или из консоли: приватные ключи кошельков
  # в terraform-состоянии НЕ хранятся — только ссылка на KMS-ключ.
  secret_string = jsonencode({
    JWT_SECRET           = random_password.jwt_secret.result
    FLASHBOTS_AUTH_KEY   = var.flashbots_auth_key
    BLOXROUTE_AUTH_HEADER = var.bloxroute_auth_header
    ETH_RPC_HTTP         = var.eth_rpc_http
    ARB_RPC_HTTP         = var.arb_rpc_http
    BASE_RPC_HTTP        = var.base_rpc_http
  })
}

variable "flashbots_auth_key" {
  description = "Ключ авторизации Flashbots (секрет: передаётся через CI-секреты)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "bloxroute_auth_header" {
  description = "Заголовок авторизации bloXroute"
  type        = string
  sensitive   = true
  default     = ""
}

variable "eth_rpc_http" {
  description = "Приватный RPC Ethereum (ключ Alchemy/QuickNode)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "arb_rpc_http" {
  description = "Приватный RPC Arbitrum"
  type        = string
  sensitive   = true
  default     = ""
}

variable "base_rpc_http" {
  description = "Приватный RPC Base"
  type        = string
  sensitive   = true
  default     = ""
}

# --------------------------------------------------------------------------- #
# Postgres (транзакционные данные) и бэкапы
# --------------------------------------------------------------------------- #
resource "aws_db_subnet_group" "main" {
  name       = "flashhunter-db"
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_instance" "postgres" {
  identifier     = "flashhunter-${var.environment}"
  engine         = "postgres"
  engine_version = "16.4"
  instance_class = var.db_instance_class

  allocated_storage     = 100
  max_allocated_storage = 500
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.keys.arn

  db_name  = "flashhunter"
  username = "flashhunter"
  password = random_password.db_password.result
  port     = 5432

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.hotpath.id]

  multi_az                = true  # отказ узла не должен останавливать учёт сделок
  backup_retention_period = var.backup_retention_days
  backup_window           = "02:00-03:00"
  maintenance_window      = "sun:03:30-sun:04:30"
  deletion_protection     = true
  skip_final_snapshot     = false
  final_snapshot_identifier = "flashhunter-${var.environment}-final"

  performance_insights_enabled = true

  tags = { Name = "flashhunter-postgres" }
}

resource "random_password" "db_password" {
  length  = 32
  special = false # спецсимволы ломают DSN в некоторых драйверах
}

resource "aws_s3_bucket" "backups" {
  bucket = "flashhunter-backups-${var.environment}"
  tags   = { Name = "flashhunter-backups" }
}

resource "aws_s3_bucket_versioning" "backups" {
  bucket = aws_s3_bucket.backups.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id

  rule {
    id     = "rotate-backups"
    status = "Enabled"

    transition {
      days          = 30
      storage_class = "STANDARD_IA" # старые бэкапы дешевле в IA
    }

    expiration {
      days = 400
    }

    noncurrent_version_expiration {
      days = 90
    }
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.keys.arn
    }
  }
}

resource "aws_s3_bucket_public_access_block" "backups" {
  bucket                  = aws_s3_bucket.backups.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# --------------------------------------------------------------------------- #
# DNS панели (Cloudflare)
# --------------------------------------------------------------------------- #
resource "cloudflare_record" "panel" {
  zone_id = var.cloudflare_zone_id
  name    = var.zone_name
  type    = "A"
  content = aws_eip.panel.public_ip
  proxied = true  # трафик идёт через Cloudflare: скрыт origin + WAF
  ttl     = 1
}

variable "cloudflare_zone_id" {
  description = "ID зоны Cloudflare для панели"
  type        = string
  default     = ""
}

resource "aws_eip" "panel" {
  domain = "vpc"
  tags   = { Name = "flashhunter-panel-eip" }
}
