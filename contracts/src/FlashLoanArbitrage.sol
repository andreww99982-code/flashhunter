// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IERC20} from "./interfaces/IERC20.sol";
import {IFlashLoanSimpleReceiver, IPool, IPoolAddressesProvider} from "./interfaces/IAaveV3Pool.sol";
import {IUniswapV2Router02} from "./interfaces/IUniswapV2Router02.sol";
import {SafeERC20Lite} from "./libraries/SafeERC20Lite.sol";

/// @title FlashLoanArbitrage
/// @notice Ядро арбитражной стратегии FlashHunter: занимает актив во Aave V3,
///         выполняет цепочку свопов и возвращает долг + комиссию в одной транзакции.
/// @dev Модель доверия: контракт вызывается только доверенным executor-кошельком
///      (роль keeper). Пользовательские средства внутри не хранятся (кроме прибыли до sweep).
contract FlashLoanArbitrage is IFlashLoanSimpleReceiver {
    using SafeERC20Lite for IERC20;

    /// @notice Владелец контракта (мультисиг в продакшне).
    address public owner;
    /// @notice Keeper (горячий кошелёк бота), которому разрешён запуск арбитража.
    address public keeper;
    /// @notice Провайдер адресов Aave V3.
    IPoolAddressesProvider public immutable addressesProvider;
    /// @notice Адрес WETH сети (для разгазовки и работы с нативным балансом).
    address public immutable wETH;
    /// @notice Контракт на паузе: экстренная остановка всех операций.
    bool public paused;
    /// @notice Кумулятивная прибыль по токенам (снимается владельцем через sweep).
    mapping(address => uint256) public accumulatedProfit;

    /// @notice One-hop арбитраж: купить на buyRouter, продать на sellRouter.
    struct ArbParams {
        address buyRouter;      // маршрутизатор покупки (пул, где актив дешевле)
        address sellRouter;     // маршрутизатор продажи
        address[] buyPath;      // путь свопа покупки (asset -> target)
        address[] sellPath;     // путь свопа продажи (target -> asset)
        uint256 minProfit;      // минимальная прибыль в asset (защита от сэндвича и проскальзывания)
        uint256 deadline;       // крайний срок исполнения (защита от задержки включения в блок)
    }

    /// @notice Логируем каждый исполненный арбитраж (для аналитики и аудита).
    event ArbitrageExecuted(address indexed asset, uint256 borrowed, uint256 premium, uint256 profit);
    /// @notice Смена keeper.
    event KeeperUpdated(address indexed keeper);
    /// @notice Пауза/возобновление.
    event PausedSet(bool paused);
    /// @notice Снятие прибыли владельцем.
    event ProfitSwept(address indexed token, address indexed to, uint256 amount);

    error NotOwner();
    error NotKeeper();
    error ContractPaused();
    error NotSelfInitiated();
    error NotAavePool();
    error Unprofitable(uint256 profit, uint256 minProfit);
    error DeadlineExpired();
    error BadPath();

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    modifier onlyKeeper() {
        // Владелец всегда имеет права keeper-а: удобно для ручных операций и тестов
        if (msg.sender != keeper && msg.sender != owner) revert NotKeeper();
        _;
    }

    modifier whenNotPaused() {
        if (paused) revert ContractPaused();
        _;
    }

    /// @param provider Адрес PoolAddressesProvider Aave V3 в целевой сети.
    /// @param weth Адрес WETH (или WMATIC/WAVAX для соответствующих сетей).
    /// @param initialKeeper Горячий кошелёк бота.
    constructor(address provider, address weth, address initialKeeper) {
        owner = msg.sender;
        addressesProvider = IPoolAddressesProvider(provider);
        wETH = weth;
        keeper = initialKeeper;
        emit KeeperUpdated(initialKeeper);
    }

    // --------------------------------------------------------------------- //
    // Управление
    // --------------------------------------------------------------------- //

    /// @notice Назначает нового keeper-а (горячий кошелёк бота).
    function setKeeper(address newKeeper) external onlyOwner {
        keeper = newKeeper;
        emit KeeperUpdated(newKeeper);
    }

    /// @notice Ставит контракт на паузу или снимает её.
    function setPaused(bool value) external onlyOwner {
        paused = value;
        emit PausedSet(value);
    }

    /// @notice Передаёт владение (двухшаговая схема реализуется на уровне мультисига).
    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "zero owner");
        owner = newOwner;
    }

    // --------------------------------------------------------------------- //
    // Основной поток арбитража
    // --------------------------------------------------------------------- //

    /// @notice Запускает арбитраж с флеш-кредитом Aave V3.
    /// @param asset Токен, который занимаем (обычно стартовая валюта маршрута).
    /// @param amount Сумма займа.
    /// @param params Параметры маршрута (см. ArbParams).
    function executeArbitrage(address asset, uint256 amount, ArbParams calldata params)
        external
        onlyKeeper
        whenNotPaused
    {
        // Занимаем актив и получаем колбэк executeOperation в той же транзакции
        IPool(addressesProvider.getPool()).flashLoanSimple(address(this), asset, amount, abi.encode(params), 0);
    }

    /// @notice Колбэк Aave V3: здесь выполняется вся арбитражная логика.
    /// @dev Функция обязана вернуть true и к моменту возврата — иметь на балансе
    ///      `amount + premium`, иначе транзакция ревертится целиком (риска для
    ///      заёмщика нет: флеш-кредит атомарен).
    function executeOperation(address asset, uint256 amount, uint256 premium, address initiator, bytes calldata params)
        external
        override
        returns (bool)
    {
        if (msg.sender != addressesProvider.getPool()) revert NotAavePool();
        if (initiator != address(this)) revert NotSelfInitiated();

        ArbParams memory arb = abi.decode(params, (ArbParams));
        if (arb.deadline != 0 && block.timestamp > arb.deadline) revert DeadlineExpired();
        if (arb.buyPath.length < 2 || arb.sellPath.length < 2) revert BadPath();
        if (arb.buyPath[0] != asset || arb.sellPath[arb.sellPath.length - 1] != asset) revert BadPath();

        uint256 startBalance = IERC20(asset).balanceOf(address(this));

        // 1. Своп покупки: asset -> target на пуле, где актив дешевле
        IERC20(asset).forceApprove(arb.buyRouter, amount);
        uint256[] memory bought = IUniswapV2Router02(arb.buyRouter).swapExactTokensForTokens(
            amount,
            0, // минимальный выход не задаём: итоговая проверка прибыли ниже строже
            arb.buyPath,
            address(this),
            arb.deadline == 0 ? block.timestamp : arb.deadline
        );
        uint256 targetAmount = bought[bought.length - 1];

        // 2. Своп продажи: target -> asset на пуле, где актив дороже
        address targetToken = arb.sellPath[0];
        IERC20(targetToken).forceApprove(arb.sellRouter, targetAmount);
        IUniswapV2Router02(arb.sellRouter).swapExactTokensForTokens(
            targetAmount,
            0,
            arb.sellPath,
            address(this),
            arb.deadline == 0 ? block.timestamp : arb.deadline
        );

        // 3. Проверка экономики: должны покрыть займ, комиссию и принести minProfit
        uint256 endBalance = IERC20(asset).balanceOf(address(this));
        uint256 owed = amount + premium;
        if (endBalance + startBalance < owed + arb.minProfit) {
            revert Unprofitable(endBalance + startBalance, owed + arb.minProfit);
        }

        uint256 profit = endBalance + startBalance - owed;
        accumulatedProfit[asset] += profit;

        // 4. Одобряем Aave списание долга (pull-модель флеш-кредита)
        IERC20(asset).forceApprove(msg.sender, owed);
        emit ArbitrageExecuted(asset, amount, premium, profit);
        return true;
    }

    // --------------------------------------------------------------------- //
    // Прибыль и аварийные операции
    // --------------------------------------------------------------------- //

    /// @notice Снимает накопленную прибыль (обычно вызывается автосивипом бота).
    function sweep(address token, address to, uint256 amount) external onlyOwner {
        uint256 available = IERC20(token).balanceOf(address(this));
        uint256 value = amount == 0 || amount > available ? available : amount;
        IERC20(token).safeTransfer(to, value);
        if (value <= accumulatedProfit[token]) {
            accumulatedProfit[token] -= value;
        } else {
            accumulatedProfit[token] = 0;
        }
        emit ProfitSwept(token, to, value);
    }

    /// @notice Разгазовка: снимает WETH в нативный токен и отправляет владельцу.
    function unwrapAndSend(uint256 amount) external onlyOwner {
        address weth = wETH;
        uint256 available = IERC20(weth).balanceOf(address(this));
        uint256 value = amount == 0 || amount > available ? available : amount;
        (bool ok,) = weth.call(abi.encodeWithSignature("withdraw(uint256)", value));
        require(ok, "withdraw failed");
        (ok,) = owner.call{value: value}("");
        require(ok, "transfer failed");
    }

    /// @notice Позволяет владельцу выполнить произвольный вызов (экстренное спасение средств).
    /// @dev Ограничено ролью owner; в продакшне owner — мультисиг с таймлоком.
    function emergencyCall(address target, uint256 value, bytes calldata data) external onlyOwner returns (bytes memory) {
        (bool ok, bytes memory result) = target.call{value: value}(data);
        require(ok, "emergency call failed");
        return result;
    }

    /// @notice Принимает нативный токен (нужно для unwrapAndSend).
    receive() external payable {}

    /// @notice Резерв газа: контракт не должен держать посторонние токены после сделки.
    function rescueToken(address token, address to) external onlyOwner {
        uint256 balance = IERC20(token).balanceOf(address(this));
        if (balance > 0) {
            IERC20(token).safeTransfer(to, balance);
        }
    }
}
