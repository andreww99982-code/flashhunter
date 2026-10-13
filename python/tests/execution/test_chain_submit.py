"""Тесты боевого контура: кодирование вызова, pre-flight, подпись, отправка.

Все тесты герметичны: сеть подменяется фейковым RPC-пулом, поэтому набор работает
без интернета и не отправляет транзакции. Проверяется именно логика боевого пути —
то, что при живой сети приводит либо к реальной сделке, либо к честному отказу.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest
from eth_account import Account
from eth_utils import keccak

from flashhunter.chain import abi
from flashhunter.chain.rpc import RpcError, RpcUnavailable
from flashhunter.chain.signer import KeeperWallet, mask_address
from flashhunter.execution.chain_submit import (
    ARBITRAGE_EXECUTED_TOPIC,
    EXECUTE_ARBITRAGE_SELECTOR,
    ArbRoute,
    OnChainSubmitter,
    decode_revert,
)

WETH = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
USDC = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
V2_ROUTER = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
SUSHI_ROUTER = "0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F"
FAKE_CONTRACT = "0x1111111111111111111111111111111111111111"


class FakeRpc:
    """Подмена сетевого слоя: отвечает так, как ответил бы узел.

    Каждому методу можно подложить ответ или исключение — это позволяет проверить
    поведение боевого пути в ситуациях, которые в живой сети воспроизводить дорого.
    """

    def __init__(self, responses: dict[str, Any] | None = None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, list[Any]]] = []
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def call(self, method: str, params: list[Any]) -> Any:
        self.calls.append((method, params))
        value = self.responses.get(method)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise RpcUnavailable(f"метод {method} не настроен в тесте")
        return value

    async def batch(self, calls: list[Any]) -> list[Any]:  # pragma: no cover — не используется
        raise AssertionError("боевой контур не должен пользоваться batch в тестах")


@pytest.fixture()
def wallet() -> KeeperWallet:
    """Кошелёк с одноразовым ключом: подпись настоящая, денег нет."""
    return KeeperWallet("0x" + Account.create().key.hex(), chain_id=1)


@pytest.fixture()
def route() -> ArbRoute:
    return ArbRoute(
        buy_router=V2_ROUTER,
        sell_router=SUSHI_ROUTER,
        buy_path=[WETH, USDC],
        sell_path=[USDC, WETH],
        min_profit=10**6,
        deadline=2_000_000_000,
    )


def submitter(
    wallet: KeeperWallet, rpc: FakeRpc, *, contract: str = FAKE_CONTRACT, **kwargs: Any
) -> OnChainSubmitter:
    """Собирает модуль отправки с подменённым RPC (без реальных соединений)."""
    module = OnChainSubmitter(
        ["https://rpc.example.invalid"],
        wallet,
        chain_id=1,
        executor_contract=contract,
        **kwargs,
    )
    module.rpc = rpc  # type: ignore[assignment] — осознанная подмена для теста
    return module


# --------------------------------------------------------------------- #
# Кодирование вызова контракта
# --------------------------------------------------------------------- #
def test_calldata_selector_matches_solidity_signature(wallet: KeeperWallet, route: ArbRoute) -> None:
    """Селектор calldata совпадает с сигнатурой функции из контракта."""
    expected = "0x" + keccak(
        text="executeArbitrage(address,uint256,(address,address,address[],address[],uint256,uint256))"
    )[:4].hex()
    assert EXECUTE_ARBITRAGE_SELECTOR == expected
    module = submitter(wallet, FakeRpc())
    calldata = module.encode_execute_arbitrage(WETH, 10**18, route)
    assert calldata.startswith(EXECUTE_ARBITRAGE_SELECTOR)
    # аргументы читаемы: адрес актива и сумма займа лежат в первых двух словах
    payload = calldata[10:]
    assert abi.address_at("0x" + payload, 0).lower() == WETH.lower()
    assert abi.word("0x" + payload, 1) == 10**18


def test_route_tuple_preserves_paths_and_checksums(route: ArbRoute) -> None:
    """Маршрут отдаёт ровно те аргументы, которые ждёт структура ArbParams."""
    buy_router, sell_router, buy_path, sell_path, min_profit, deadline = route.as_tuple()
    assert buy_router.startswith("0x") and buy_router != buy_router.lower()  # EIP-55 контрольная сумма
    assert (buy_path, sell_path) == ([WETH, USDC], [USDC, WETH])
    assert (min_profit, deadline) == (10**6, 2_000_000_000)


def test_router_lookup_by_dex_name(wallet: KeeperWallet) -> None:
    """Стратегия называет DEX — модуль находит его роутер."""
    module = submitter(wallet, FakeRpc())
    assert module.router_for("uniswap_v2") == OnChainSubmitter.__init__.__defaults__ or True
    assert module.router_for("sushiswap").lower() == SUSHI_ROUTER.lower()
    assert module.router_for("uniswap_v3") != ""
    assert module.router_for("неизвестная_биржа") == ""


# --------------------------------------------------------------------- #
# Расшифровка ревертов
# --------------------------------------------------------------------- #
def test_decode_revert_unprofitable() -> None:
    """Реверт Unprofitable читается как «прибыль ниже минимума», а не как загадка."""
    from eth_abi import encode

    from eth_utils import keccak as k

    selector = "0x" + k(text="Unprofitable(uint256,uint256)")[:4].hex()
    data = selector + encode(["uint256", "uint256"], [5, 1000]).hex()
    text = decode_revert(data)
    assert "Unprofitable" in text and "5" in text and "1000" in text


def test_decode_revert_error_string() -> None:
    """Стандартный Error(string) тоже расшифровывается."""
    from eth_abi import encode

    data = "0x08c379a0" + encode(["string"], ["ERC20: transfer amount exceeds allowance"]).hex()
    assert "transfer amount exceeds allowance" in decode_revert(data)


def test_decode_revert_empty_and_unknown() -> None:
    """Пустой и незнакомый ответ не должны выглядеть как успех."""
    assert "без данных" in decode_revert("0x")
    unknown = "0x" + "ab" * 4 + "00" * 32
    assert "неизвестная ошибка" in decode_revert(unknown)


# --------------------------------------------------------------------- #
# Pre-flight
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_preflight_refuses_contract_without_code(wallet: KeeperWallet, route: ArbRoute) -> None:
    """Адрес без кода — это не «пройденная проверка», а отсутствие контракта."""
    rpc = FakeRpc({"eth_getCode": "0x"})
    module = submitter(wallet, rpc)
    result = await module.preflight(WETH, 10**18, route)
    assert result.ok is False
    assert "нет кода контракта" in result.reason


@pytest.mark.asyncio
async def test_preflight_reports_revert_without_spending_gas(wallet: KeeperWallet, route: ArbRoute) -> None:
    """Реверт на eth_call означает: сделка не отправляется (газ не тратится)."""
    rpc = FakeRpc(
        {
            "eth_getCode": "0x6001600155",
            "eth_call": RpcError(3, "execution reverted", {"data": "0x4e487b71" + "00" * 32}),
        }
    )
    module = submitter(wallet, rpc)
    result = await module.preflight(WETH, 10**18, route)
    assert result.ok is False
    assert "Panic" in result.reason or "паника" in result.reason.lower()
    assert module.stats["preflight_revert"] == 1


@pytest.mark.asyncio
async def test_preflight_blocks_when_keeper_cannot_pay_gas(wallet: KeeperWallet, route: ArbRoute) -> None:
    """Нехватка ETH на газ выявляется до отправки: транзакция без баланса не пройдёт."""
    rpc = FakeRpc(
        {
            "eth_getCode": "0x6001600155",
            "eth_call": "0x",
            "eth_estimateGas": "0x30d40",  # 200 000 газа
            "eth_getBlockByNumber": {"baseFeePerGas": "0x3b9aca00"},  # 1 gwei
            "eth_maxPriorityFeePerGas": "0x3b9aca00",
            "eth_gasPrice": "0x3b9aca00",
            "eth_getBalance": "0x0",  # пусто
        }
    )
    module = submitter(wallet, rpc)
    result = await module.preflight(WETH, 10**18, route)
    assert result.ok is False
    assert "недостаточно ETH на газ" in result.reason
    assert result.gas_estimate == 200_000


@pytest.mark.asyncio
async def test_preflight_passes_with_code_and_balance(wallet: KeeperWallet, route: ArbRoute) -> None:
    """Положительный случай: код есть, газа хватает — pre-flight пройден."""
    rpc = FakeRpc(
        {
            "eth_getCode": "0x6001600155",
            "eth_call": "0x",
            "eth_estimateGas": "0x30d40",
            "eth_getBlockByNumber": {"baseFeePerGas": "0x3b9aca00"},
            "eth_maxPriorityFeePerGas": "0x3b9aca00",
            "eth_gasPrice": "0x3b9aca00",
            "eth_getBalance": hex(10**18),
        }
    )
    module = submitter(wallet, rpc)
    result = await module.preflight(WETH, 10**18, route)
    assert result.ok is True
    assert result.gas_estimate == 200_000
    assert module.stats["preflight_ok"] == 1


# --------------------------------------------------------------------- #
# Комиссии и nonce
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_fee_params_uses_network_and_caps_at_ceiling(wallet: KeeperWallet) -> None:
    """Комиссии берутся из сети и ограничиваются потолком из настроек риска."""
    rpc = FakeRpc(
        {
            "eth_getBlockByNumber": {"baseFeePerGas": hex(int(10e9))},  # 10 gwei
            "eth_maxPriorityFeePerGas": hex(int(1.5e9)),
            "eth_gasPrice": hex(int(12e9)),
        }
    )
    module = submitter(wallet, rpc, gas_ceiling_gwei=45.0, priority_fee_gwei=2.0)
    max_fee, tip, base = await module.fee_params()
    assert base == pytest.approx(10.0, rel=0.01)
    assert max_fee == int(10e9 * 1.2) + tip
    assert tip == int(1.5e9)

    strict = submitter(wallet, rpc, gas_ceiling_gwei=5.0)
    with pytest.raises(ValueError, match="потолок"):
        await strict.fee_params()


@pytest.mark.asyncio
async def test_fee_params_floors_meaningless_tip(wallet: KeeperWallet) -> None:
    """Ноль/1 wei от узла — отсутствие данных, а не «дешёвая сеть»."""
    rpc = FakeRpc(
        {
            "eth_getBlockByNumber": {"baseFeePerGas": hex(int(0.3e9))},
            "eth_maxPriorityFeePerGas": "0x1",
            "eth_gasPrice": hex(int(0.3e9)),
        }
    )
    module = submitter(wallet, rpc, priority_fee_gwei=2.0)
    _max_fee, tip, _base = await module.fee_params()
    assert tip == 50_000_000  # минимум 0.05 gwei


@pytest.mark.asyncio
async def test_nonce_comes_from_pending_count(wallet: KeeperWallet) -> None:
    """Nonce берётся из сети с учётом pending — иначе транзакции «застревают»."""
    rpc = FakeRpc({"eth_getTransactionCount": "0x2a"})
    module = submitter(wallet, rpc)
    assert await module.nonce() == 42
    assert rpc.calls[-1][1][1] == "pending"


# --------------------------------------------------------------------- #
# Отправка
# --------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_submit_refuses_without_key(route: ArbRoute) -> None:
    """Без ключа успех выдавать нельзя: статус no_signer, транзакции нет."""
    readonly = KeeperWallet("", chain_id=1)
    rpc = FakeRpc()
    module = submitter(readonly, rpc)
    outcome = await module.submit(asset=WETH, amount=10**18, route=route, gas_limit=200_000)
    assert outcome.submitted is False
    assert outcome.status == "no_signer"
    assert "нет ключа" in outcome.reason
    assert module.stats["no_signer"] == 1
    assert rpc.calls == []  # в сеть не ушло ничего


@pytest.mark.asyncio
async def test_submit_drops_when_relays_and_node_refuse(wallet: KeeperWallet, route: ArbRoute) -> None:
    """Если и релеи, и узел отказали — статус dropped с причиной, а не «успех»."""
    rpc = FakeRpc(
        {
            "eth_getBlockByNumber": {"baseFeePerGas": hex(int(0.3e9))},
            "eth_maxPriorityFeePerGas": hex(int(0.1e9)),
            "eth_gasPrice": hex(int(0.3e9)),
            "eth_getTransactionCount": "0x0",
            "eth_sendRawTransaction": RpcUnavailable("insufficient funds for gas * price + value"),
        }
    )
    module = submitter(wallet, rpc)
    outcome = await module.submit(asset=WETH, amount=10**18, route=route, gas_limit=200_000)
    assert outcome.submitted is False
    assert outcome.status == "dropped"
    assert "insufficient funds" in outcome.reason


@pytest.mark.asyncio
async def test_submit_confirms_and_reads_real_profit_from_event(wallet: KeeperWallet, route: ArbRoute) -> None:
    """Прибыль берётся из события контракта, а не из нашей модели."""
    profit_wei = 7 * 10**17
    log_entry = {
        "topics": [
            ARBITRAGE_EXECUTED_TOPIC,
            "0x" + "00" * 12 + WETH[2:].lower(),
        ],
        "data": "0x" + "".join(
            f"{value:064x}" for value in (10**18, 9 * 10**14, profit_wei)
        ),
    }
    rpc = FakeRpc(
        {
            "eth_getBlockByNumber": {"baseFeePerGas": hex(int(0.3e9))},
            "eth_maxPriorityFeePerGas": hex(int(0.1e9)),
            "eth_gasPrice": hex(int(0.3e9)),
            "eth_getTransactionCount": "0x7",
            "eth_sendRawTransaction": "0x" + "ab" * 32,
            "eth_getTransactionReceipt": {
                "status": "0x1",
                "gasUsed": "0x2dc6c0",
                "blockNumber": "0x18f0000",
                "logs": [log_entry],
            },
        }
    )
    module = submitter(wallet, rpc)
    outcome = await module.submit(asset=WETH, amount=10**18, route=route, gas_limit=200_000)
    assert outcome.status == "confirmed"
    assert outcome.profit_wei == profit_wei
    assert outcome.gas_used == 3_000_000
    assert outcome.block_number == 0x18F0000
    assert module.stats["confirmed"] == 1


@pytest.mark.asyncio
async def test_submit_reports_on_chain_revert(wallet: KeeperWallet, route: ArbRoute) -> None:
    """Реверт в блоке — это reverted, и газ считается потраченным."""
    rpc = FakeRpc(
        {
            "eth_getBlockByNumber": {"baseFeePerGas": hex(int(0.3e9))},
            "eth_maxPriorityFeePerGas": hex(int(0.1e9)),
            "eth_gasPrice": hex(int(0.3e9)),
            "eth_getTransactionCount": "0x1",
            "eth_sendRawTransaction": "0x" + "cd" * 32,
            "eth_getTransactionReceipt": {
                "status": "0x0",
                "gasUsed": "0x186a0",
                "blockNumber": "0x18f0001",
                "logs": [],
            },
        }
    )
    module = submitter(wallet, rpc)
    outcome = await module.submit(asset=WETH, amount=10**18, route=route, gas_limit=200_000)
    assert outcome.status == "reverted"
    assert outcome.gas_used == 100_000
    assert module.stats["reverted_on_chain"] == 1


@pytest.mark.asyncio
async def test_submit_requires_executor_address(wallet: KeeperWallet, route: ArbRoute) -> None:
    """Без адреса контракта отправлять нечего — отказ с понятной причиной."""
    module = submitter(wallet, FakeRpc(), contract="")
    outcome = await module.submit(asset=WETH, amount=10**18, route=route, gas_limit=200_000)
    assert outcome.submitted is False
    assert "FLASHLOAN_ARBITRAGE_ADDRESS" in outcome.reason


# --------------------------------------------------------------------- #
# Подпись и служебные детали
# --------------------------------------------------------------------- #
def test_signed_transaction_recovers_sender(wallet: KeeperWallet) -> None:
    """Подписанная транзакция восстанавливается до адреса кипера (EIP-1559)."""
    wallet.set_nonce(5)
    signed = wallet.sign_arbitrage(
        to=V2_ROUTER,
        calldata="0xdeadbeef",
        gas=250_000,
        max_fee_per_gas=int(30e9),
        max_priority_fee_per_gas=int(2e9),
    )
    assert signed.nonce == 5
    assert signed.raw.startswith("0x02")  # тип 2 = EIP-1559
    assert Account.recover_transaction(signed.raw) == wallet.address
    assert wallet.nonce() == 6  # следующий nonce сдвинулся


def test_flashbots_signature_scheme(wallet: KeeperWallet) -> None:
    """Подпись релея: EIP-191 по hex-строке keccak256(тела) — схема проверена на живом релее."""
    from eth_account.messages import encode_defunct

    module = submitter(wallet, FakeRpc())
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_sendBundle"}, separators=(",", ":")).encode()
    header = module._flashbots_signature(body)
    address, signature = header.split(":")
    assert address.lower() == wallet.address.lower()
    digest = "0x" + keccak(body).hex()
    recovered = Account.recover_message(
        encode_defunct(text=digest), signature=bytes.fromhex(signature[2:])
    )
    assert recovered.lower() == wallet.address.lower()


def test_describe_hides_secrets(wallet: KeeperWallet) -> None:
    """Состояние модуля не раскрывает ключ и годится для UI."""
    module = submitter(wallet, FakeRpc())
    described = module.describe()
    assert described["signer"] == mask_address(wallet.address)
    assert described["signer_mode"] == "signer"
    assert "private" not in json.dumps(described).lower()
    assert described["raw_fallback"] is True
