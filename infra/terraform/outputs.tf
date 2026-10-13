output "instance_id" {
  description = "ID EC2-инстанса горячего пути (для ансибл-инвентори и мониторинга)"
  value       = aws_instance.hotpath.id
}

output "instance_private_ip" {
  description = "Приватный IP инстанса: доступ только через VPN/bastion"
  value       = aws_instance.hotpath.private_ip
}

output "panel_url" {
  description = "Адрес панели управления"
  value       = "https://${var.zone_name}"
}

output "database_endpoint" {
  description = "Точка подключения Postgres (используется в POSTGRES_DSN)"
  value       = aws_db_instance.postgres.endpoint
  sensitive   = true
}

output "runtime_secret_arn" {
  description = "ARN секрета рантайма: инстанс читает его через instance profile"
  value       = aws_secretsmanager_secret.runtime.arn
}

output "backup_bucket" {
  description = "Бакет бэкапов (используется в CronJob flashhunter-backup)"
  value       = aws_s3_bucket.backups.bucket
}

output "kms_key_arn" {
  description = "KMS-ключ для шифрования ключей и бэкапов"
  value       = aws_kms_key.keys.arn
}
