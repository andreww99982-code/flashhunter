# Ansible: деплой FlashHunter на ноду горячего пути

## Что делает плейбук

1. **Роль `common`** — базовая защита: вход только по ключам, fail2ban, auditd на файлы
   ключей, тюнинг ядра под низкую латентность (busy-poll, TCP fastopen), автообновления.
2. **Роль `docker`** — Docker Engine + Compose v2 из официального репозитория,
   лог-ротация, пул адресов без конфликта с VPC.
3. **Роль `flashhunter`** — checkout нужной версии, выгрузка секретов из AWS Secrets
   Manager (включая приватный ключ keeper, права `0400`), генерация `.env`,
   `docker compose up -d --build`, затем **проверка, что бот реально обрабатывает блоки**
   (сравниваются два замера `blocks_processed`).

## Запуск

```bash
cd infra/ansible

# 1. Секреты: создайте и заполните vault
cp group_vars/vault.yml.example group_vars/all/vault.yml
ansible-vault encrypt group_vars/all/vault.yml
echo "пароль-от-вальта" > .vault_pass && chmod 600 .vault_pass   # в .gitignore

# 2. Проверка соединения и инвентаря
ansible -i inventory.ini hotpath -m ping

# 3. Dry-run: полный прогон без изменений
ansible-playbook -i inventory.ini playbook.yml --check --diff

# 4. Деплой конкретной версии
ansible-playbook -i inventory.ini playbook.yml -e flashhunter_version=0.1.7

# 5. Переключение в боевой режим (только после 24 ч dry-run по регламенту ТЗ §16!)
ansible-playbook -i inventory.ini playbook.yml -e flashhunter_dry_run=false --tags app,deploy
```

## Обкатка перед боем

| Шаг | Что проверяем | Критерий перехода дальше |
|-----|---------------|--------------------------|
| 1 | Деплой в `dry_run=true` | API отвечает 200, `blocks_processed` растёт |
| 2 | 24 часа наблюдения | ≥ 0 ложных срабатываний kill-switch, отчёты в `/backtest` положительные |
| 3 | Тестовая сделка в dry-run | Симуляция проходит без реверта, латентность p95 ≤ бюджета |
| 4 | Микро-депозит ($50–100 на keeper) | Первая реальная сделка исполняется, сделка видна в `/history` |
| 5 | Полный банк | После недели без инцидентов |

## Откат

```bash
# Откат к предыдущей версии: git-роль ставит нужный тег, compose пересобирает
ansible-playbook -i inventory.ini playbook.yml -e flashhunter_version=0.1.6

# Экстренная остановка торговли без отката (панель или API)
curl -X POST https://flashhunter.example.com/api/v1/settings/kill-switch \
  -H "Authorization: Bearer $TOKEN" -d '{"active": true, "reason": "ручная остановка"}'
```
