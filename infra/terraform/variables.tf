variable "region" {
  description = "AWS-регион размещения (близость к RPC-провайдеру критична для латентности)"
  type        = string
  default     = "eu-west-1"
}

variable "environment" {
  description = "Окружение: prod | staging"
  type        = string
  default     = "prod"
}

variable "owner_email" {
  description = "Ответственный за инфраструктуру (теги, алерты)"
  type        = string
}

variable "zone_name" {
  description = "Домен панели (Cloudflare DNS)"
  type        = string
  default     = "flashhunter.example.com"
}

variable "instance_type" {
  description = "Тип EC2 для горячего пути. c7i.large: 2 vCPU, высокий single-thread — критично для обработки блока за 5 мс"
  type        = string
  default     = "c7i.large"
}

variable "keeper_public_keys" {
  description = "SSH-ключи для доступа к инстансу (доступ только по ключу, пароль выключен)"
  type        = list(string)
  default     = []
}

variable "db_instance_class" {
  description = "Класс RDS для Postgres"
  type        = string
  default     = "db.t4g.medium"
}

variable "backup_retention_days" {
  description = "Срок хранения резервных копий (дней)"
  type        = number
  default     = 30
}

variable "allowed_admin_cidrs" {
  description = "CIDR, с которых разрешён SSH и доступ к панели. По умолчанию — только офис/VPN"
  type        = list(string)
  default     = ["10.0.0.0/8"]
}
