# Kubernetes: развёртывание FlashHunter в кластере

Манифесты рассчитаны на кластер с установленными:
`ingress-nginx`, `cert-manager`, `secrets-store-csi-driver`, `kube-prometheus-stack`.

## Порядок применения

```bash
kubectl apply -f infra/k8s/00-namespace.yaml
kubectl apply -f infra/k8s/01-configmap.yaml
# Секреты создаются из внешнего хранилища, шаблон 02-secret.yaml — только для примера:
kubectl create secret generic flashhunter-secrets -n flashhunter \
  --from-literal=JWT_SECRET="$(openssl rand -hex 32)" \
  --from-literal=ADMIN_DEFAULT_PASSWORD="$(openssl rand -base64 18)" \
  --from-literal=ETH_RPC_HTTP="$ETH_RPC_HTTP" \
  --from-literal=FLASHBOTS_AUTH_KEY="$FLASHBOTS_AUTH_KEY"

kubectl apply -f infra/k8s/10-api-deployment.yaml
kubectl apply -f infra/k8s/11-api-service.yaml
kubectl apply -f infra/k8s/12-ingress.yaml
kubectl apply -f infra/k8s/20-hpa.yaml
kubectl apply -f infra/k8s/21-pdb.yaml
kubectl apply -f infra/k8s/22-networkpolicy.yaml
kubectl apply -f infra/k8s/30-relays-worker.yaml
kubectl apply -f infra/k8s/40-cron-backtest.yaml
kubectl apply -f infra/k8s/41-cron-backup.yaml
kubectl apply -f infra/k8s/50-servicemonitor.yaml
```

## Ключевые решения

| Решение | Причина |
|---------|---------|
| `replicas: 2` + лидер-лок в Redis | Горячий путь должен работать при падении пода; при этом bundle отправляет только лидер, иначе дубли сделок |
| `maxSurge: 0` при обновлении | Третья реплика бессмысленна (лидер один) и создаёт конкуренцию за nonce |
| `readOnlyRootFilesystem: true` | Контейнер ничего не пишет в ФС: утечка кода/ключей невозможна |
| Ключи кошельков через CSI из KMS | Приватные ключи не попадают ни в git, ни в `Secret` — см. `docs/adr/0006-keys-in-kms.md` |
| `NetworkPolicy` с `ipBlock.except` | Бот не должен ходить во внутреннюю сеть кластера (защита от SSRF на RPC-прокси) |
| `PodDisruptionBudget: minAvailable 1` | Обновление нод не должно останавливать торговлю |
| Ночной CronJob-бэктест | Ловит регрессии стратегий до того, как они стоят денег |
