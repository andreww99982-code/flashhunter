// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IERC20} from "./interfaces/IERC20.sol";
import {SafeERC20Lite} from "./libraries/SafeERC20Lite.sol";

/// @title BridgeAdapter
/// @notice Адаптер кросс-чейн интентов: пользователь объявляет намерение
///         «отдать X в сети A — получить минимум Y в сети B», солвер (наш бот)
///         исполняет его за комиссию.
/// @dev Комиссия ограничена сверху (MAX_FEE_BPS): политика проекта — честный
///      интент-флоу, а не «грабёж» пользователей. Интенты истекают по времени.
contract BridgeAdapter {
    using SafeERC20Lite for IERC20;

    /// @notice Владелец контракта.
    address public owner;
    /// @notice Keeper (горячий кошелёк бота).
    address public keeper;
    /// @notice Максимальная комиссия солвера, bps (1 bps = 0.01%). 25 bps = 0.25%.
    uint16 public constant MAX_FEE_BPS = 25;
    /// @notice Реферер-комиссия не предусмотрена: базовая ставка 1 bp.
    uint16 public constant MIN_FEE_BPS = 1;
    /// @notice Пауза.
    bool public paused;

    /// @notice Статус интента.
    enum IntentStatus {
        None,
        Open,
        Filled,
        Cancelled
    }

    /// @notice Интент пользователя (офчейн-ордер, зафиксированный ончейн).
    struct Intent {
        address user;            // кто объявил интент
        address tokenIn;         // что отдаёт (в этой сети)
        uint256 amountIn;        // сколько отдаёт
        uint256 minAmountOut;    // минимум, что хочет получить в целевой сети
        uint64 destChainId;      // целевая сеть
        address recipient;       // получатель в целевой сети
        uint64 deadline;         // срок действия
        uint16 feeBps;           // комиссия солвера
        IntentStatus status;     // статус
    }

    /// @notice Интенты по id (id детерминирован от данных интента).
    mapping(bytes32 => Intent) public intents;
    /// @notice Накопленная комиссия по токену.
    mapping(address => uint256) public accumulatedFees;
    /// @notice Кумулятивная комиссия (метрика панели).
    uint256 public totalFeeUsdCents;

    event IntentCreated(bytes32 indexed intentId, address indexed user, address tokenIn, uint256 amountIn);
    event IntentFilled(bytes32 indexed intentId, address indexed solver, uint256 feeAmount);
    event IntentCancelled(bytes32 indexed intentId);
    event KeeperUpdated(address indexed keeper);
    event PausedSet(bool paused);

    error NotOwner();
    error NotKeeper();
    error NotUser();
    error ContractPaused();
    error IntentNotOpen();
    error IntentExpired();
    error FeeTooHigh(uint16 requested, uint16 maximum);
    error MinOutTooHigh();

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    modifier onlyKeeper() {
        if (msg.sender != keeper && msg.sender != owner) revert NotKeeper();
        _;
    }

    modifier whenNotPaused() {
        if (paused) revert ContractPaused();
        _;
    }

    constructor(address initialKeeper) {
        owner = msg.sender;
        keeper = initialKeeper;
        emit KeeperUpdated(initialKeeper);
    }

    /// @notice Назначает keeper-а (солвера).
    function setKeeper(address newKeeper) external onlyOwner {
        keeper = newKeeper;
        emit KeeperUpdated(newKeeper);
    }

    /// @notice Пауза/возобновление.
    function setPaused(bool value) external onlyOwner {
        paused = value;
        emit PausedSet(value);
    }

    /// @notice Идентификатор интента: детерминированная функция от параметров и nonce.
    function intentId(address user, address tokenIn, uint256 amountIn, uint64 destChainId, uint64 deadline, uint256 nonce)
        public
        pure
        returns (bytes32)
    {
        return keccak256(abi.encode(user, tokenIn, amountIn, destChainId, deadline, nonce));
    }

    /// @notice Создаёт интент: средства блокируются в контракте до исполнения или отмены.
    function createIntent(
        address tokenIn,
        uint256 amountIn,
        uint256 minAmountOut,
        uint64 destChainId,
        address recipient,
        uint64 deadline,
        uint16 feeBps,
        uint256 nonce
    ) external whenNotPaused returns (bytes32 id) {
        if (deadline <= block.timestamp) revert IntentExpired();
        if (feeBps < MIN_FEE_BPS || feeBps > MAX_FEE_BPS) revert FeeTooHigh(feeBps, MAX_FEE_BPS);
        // Защита от «невозможных» интентов: minAmountOut не должен превышать вход более чем в 10 раз
        // (грубая проверка на опечатку, точная проверка экономики — офчейн у солвера)
        if (minAmountOut > amountIn * 10) revert MinOutTooHigh();

        id = intentId(msg.sender, tokenIn, amountIn, destChainId, deadline, nonce);
        if (intents[id].status != IntentStatus.None) revert IntentNotOpen();

        intents[id] = Intent({
            user: msg.sender,
            tokenIn: tokenIn,
            amountIn: amountIn,
            minAmountOut: minAmountOut,
            destChainId: destChainId,
            recipient: recipient,
            deadline: deadline,
            feeBps: feeBps,
            status: IntentStatus.Open
        });

        IERC20(tokenIn).safeTransferFrom(msg.sender, address(this), amountIn);
        emit IntentCreated(id, msg.sender, tokenIn, amountIn);
    }

    /// @notice Исполняет интент: солвер подтверждает, что выдал средства в целевой сети,
    ///         и забирает заблокированный объём минус свою комиссию.
    /// @dev В реальной архитектуре исполнение подтверждается коллектором сообщений
    ///      (LayerZero/Wormhole/Axelar); здесь — доверенный keeper-солвер.
    function fillIntent(bytes32 id) external onlyKeeper whenNotPaused {
        Intent storage intent = intents[id];
        if (intent.status != IntentStatus.Open) revert IntentNotOpen();
        if (block.timestamp > intent.deadline) revert IntentExpired();

        uint256 fee = (intent.amountIn * intent.feeBps) / 10_000;
        intent.status = IntentStatus.Filled;
        accumulatedFees[intent.tokenIn] += fee;

        // Комиссия остаётся в контракте (снимается владельцем), остальное — солверу
        // Солвер уже отдал средства пользователю в целевой сети — это цена интента.
        IERC20(intent.tokenIn).safeTransfer(msg.sender, intent.amountIn - fee);
        emit IntentFilled(id, msg.sender, fee);
    }

    /// @notice Отмена интента пользователем: средства возвращаются целиком.
    function cancelIntent(bytes32 id) external whenNotPaused {
        Intent storage intent = intents[id];
        if (intent.status != IntentStatus.Open) revert IntentNotOpen();
        if (msg.sender != intent.user) revert NotUser();
        intent.status = IntentStatus.Cancelled;
        IERC20(intent.tokenIn).safeTransfer(intent.user, intent.amountIn);
        emit IntentCancelled(id);
    }

    /// @notice Снимает накопленные комиссии.
    function sweepFees(address token, address to, uint256 amount) external onlyOwner {
        uint256 available = accumulatedFees[token];
        uint256 value = amount == 0 || amount > available ? available : amount;
        accumulatedFees[token] = available - value;
        IERC20(token).safeTransfer(to, value);
    }

    /// @notice Аварийная отмена интента владельцем (например, при зависании моста).
    function forceCancel(bytes32 id) external onlyOwner {
        Intent storage intent = intents[id];
        if (intent.status != IntentStatus.Open) revert IntentNotOpen();
        intent.status = IntentStatus.Cancelled;
        IERC20(intent.tokenIn).safeTransfer(intent.user, intent.amountIn);
        emit IntentCancelled(id);
    }

    receive() external payable {}
}
