# Инфраструктура FlashHunter

Четыре уровня развёртывания — от «одной команды» до продакшна в AWS.

| Каталог | Уровень | Что внутри |
|---------|---------|-----------|
| `docker/` | Локально и одна нода | `Dockerfile.api` (Multi-stage: node → python), `Dockerfile.rust`, `nginx.conf`, `prometheus.yml`, `alerts.yml`, дашборд Grafana |
| `k8s/` | Кластер | Namespace, ConfigMap/Secret, Deployment API + hotpath, Service, Ingress, HPA, PDB, NetworkPolicy, CronJob (ночной бэктест и бэкапы), ServiceMonitor |
| `terraform/` | Облако | VPC, EC2 горячего пути, RDS Postgres, S3 + lifecycle, KMS, Secrets Manager, Cloudflare |
| `ansible/` | Деплой на ноду | Роли common (hardening), docker (Engine + Compose v2), flashhunter (секреты из KMS, генерация .env, запуск, верификация блоков) |

## Порядок развёртывания продакшна

```
1. terraform apply          → VPC, EC2, RDS, S3, KMS, DNS
2. ansible-playbook         → hardening, Docker, секреты, запуск стека (dry-run)
3. 24 часа dry-run          → наблюдение, отчёты бэктеста, метрики латентности
4. ansible -e dry_run=false → боевой режим с малым балансом keeper-а
5. масштабирование          → k8s-манифесты при росте нагрузки и числа сетей
```

## Что важно помнить

* **Секреты не в git.** Приватные ключи — только KMS/HSM и инжект при деплое
  (`docs/adr/0006-keys-in-kms.md`).
* **Горячий путь без публичного IP.** Входящий трафик только через bastion/VPN;
  исходящий — NAT к RPC и релеям (правила в `terraform/main.tf`).
* **Лимиты ресурсов заданы.** В k8s у `hotpath` зарезервирован CPU: горячий путь
  не должен конкурировать за ядро с фоновыми задачами.
* **Бэкапы и ретеншн настроены.** S3 с версионированием и переходом в STANDARD_IA,
  CronJob дампа Postgres раз в 6 часов, TTL телеметрии в ClickHouse.
