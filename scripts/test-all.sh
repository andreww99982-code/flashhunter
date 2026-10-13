#!/usr/bin/env bash
# FlashHunter: полный прогон тестов всех слоёв.
# Запускает то, что установлено в системе; отсутствующие тулчейны отмечает как SKIP,
# чтобы скрипт был полезен и на машине разработчика, и в CI.
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

FAILED=()
SKIPPED=()
PASSED=()

section() { echo; echo "════════ $* ════════"; }
result()  { echo "──────── $* ────────"; }

# --- 1. Python: тесты всех модулей ----------------------------------------- #
section "Python: pytest (db, risk, math, strategies, execution, api, alerting, backtest)"
if command -v python3 >/dev/null 2>&1; then
  if (cd python && python3 -m pytest -q); then
    PASSED+=("python")
  else
    FAILED+=("python")
  fi
else
  SKIPPED+=("python (нет python3)")
fi

# --- 2. Статический анализ Python ------------------------------------------ #
section "Python: ruff + mypy"
if (cd python && python3 -m ruff --version >/dev/null 2>&1); then
  if (cd python && python3 -m ruff check . && python3 -m mypy flashhunter); then
    PASSED+=("python-lint")
  else
    FAILED+=("python-lint")
  fi
else
  SKIPPED+=("python-lint (ruff/mypy не установлены: pip install -e 'python[dev]')")
fi

# --- 3. Панель: сборка + типы ---------------------------------------------- #
section "UI: TypeScript + Vite build"
if command -v npm >/dev/null 2>&1 && [[ -d ui/node_modules ]]; then
  if (cd ui && npx tsc --noEmit && npm run build --silent); then
    PASSED+=("ui")
  else
    FAILED+=("ui")
  fi
else
  SKIPPED+=("ui (нет npm или не выполнен 'cd ui && npm install')")
fi

# --- 4. Rust: тесты воркспейса --------------------------------------------- #
section "Rust: cargo test --workspace"
if command -v cargo >/dev/null 2>&1; then
  if (cd rust && cargo test --workspace --quiet); then
    PASSED+=("rust")
  else
    FAILED+=("rust")
  fi
  if (cd rust && cargo clippy --all-targets --quiet -- -D warnings); then
    PASSED+=("rust-clippy")
  else
    FAILED+=("rust-clippy")
  fi
else
  SKIPPED+=("rust (cargo не установлен: https://rustup.rs)")
fi

# --- 5. Контракты: Foundry + Echidna --------------------------------------- #
section "Solidity: forge test (+ echidna, если установлена)"
if command -v forge >/dev/null 2>&1; then
  if (cd contracts && forge build --silent && forge test -q); then
    PASSED+=("contracts")
  else
    FAILED+=("contracts")
  fi
  if command -v echidna >/dev/null 2>&1; then
    if (cd contracts && echidna echidna/EchidnaFlashHunter.sol --contract EchidnaFlashHunter --config echidna/EchidnaConfig.yaml --test-limit 2000); then
      PASSED+=("echidna")
    else
      FAILED+=("echidna")
    fi
  else
    SKIPPED+=("echidna (не установлена: https://github.com/crytic/echidna)")
  fi
else
  SKIPPED+=("contracts (forge не установлен: curl -L https://foundry.paradigm.xyz | bash)")
fi

# --- 6. Проверка compose-файла --------------------------------------------- #
section "Инфраструктура: валидация docker compose"
if command -v docker >/dev/null 2>&1; then
  if docker compose config -q; then
    PASSED+=("compose")
  else
    FAILED+=("compose")
  fi
else
  SKIPPED+=("compose (docker не установлен)")
fi

# --- Итог ------------------------------------------------------------------ #
echo
echo "════════════════ ИТОГ ════════════════"
if (( ${#PASSED[@]} )); then
  echo "Пройдено: ${PASSED[*]}"
fi
if (( ${#SKIPPED[@]} )); then
  echo "Пропущено:"
  for item in "${SKIPPED[@]}"; do echo "  - ${item}"; done
fi
if (( ${#FAILED[@]} )); then
  echo -e "\033[0;31mПровалено: ${FAILED[*]}\033[0m"
  exit 1
fi
echo -e "\033[0;32mВсе доступные проверки пройдены.\033[0m"
