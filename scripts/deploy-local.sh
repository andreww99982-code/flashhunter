#!/usr/bin/env bash
# FlashHunter: деплой контрактов в локальный Anvil (для отладки боевого пути).
#   Anvil даёт детерминированные адреса и мгновенные блоки — идеален для e2e-прогона.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR/contracts"

if ! command -v anvil >/dev/null 2>&1 || ! command -v forge >/dev/null 2>&1; then
  echo "Нужны anvil и forge (Foundry). Установка:"
  echo "  curl -L https://foundry.paradigm.xyz | bash && foundryup"
  exit 1
fi

mkdir -p deployments

echo "→ Запускаю Anvil (127.0.0.1:8545)..."
anvil --silent --host 127.0.0.1 --port 8545 &
ANVIL_PID=$!
trap 'kill $ANVIL_PID 2>/dev/null || true' EXIT
sleep 2

# Anvil-аккаунт №0 (публичный ключ из стандартного мнемоника — НЕ реальный кошелёк)
export DEPLOYER_PRIVATE_KEY="0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
export KEEPER_ADDRESS="0x70997970C51812dc3A010C7d01b50e0d17dc79C8"
# В локальном Anvil Aave V3 нет: моками служат контракты из src/mocks
export AAVE_POOL_ADDRESSES_PROVIDER="${AAVE_POOL_ADDRESSES_PROVIDER:-0x0000000000000000000000000000000000000001}"
export WETH_ADDRESS="${WETH_ADDRESS:-0x0000000000000000000000000000000000000002}"

echo "→ Деплою контракты..."
forge script script/Deploy.s.sol:Deploy --rpc-url http://127.0.0.1:8545 --broadcast -vv 2>&1 | tee /tmp/fh-deploy.log

# Сохраняем адреса: бот читает deployments/local.json при CHAIN_MODE=local
python3 - "$ROOT_DIR/contracts/deployments/local.json" <<'PY'
"""Извлекаем адреса из лога forge и пишем в deployments/local.json."""
import json
import pathlib
import re
import sys

log = pathlib.Path("/tmp/fh-deploy.log").read_text(encoding="utf-8", errors="ignore")
pattern = re.compile(r"(FlashLoanArbitrage|Liquidator|JITVault|BridgeAdapter):\s*(0x[0-9a-fA-F]{40})")
addresses = {name: addr for name, addr in pattern.findall(log)}

target = pathlib.Path(sys.argv[1])
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps({
    "chain_id": 31337,
    "rpc": "http://127.0.0.1:8545",
    "contracts": addresses,
}, ensure_ascii=False, indent=2), encoding="utf-8")
print("✔ Адреса сохранены:", target)
for name, addr in addresses.items():
    print(f"   {name}: {addr}")
PY
