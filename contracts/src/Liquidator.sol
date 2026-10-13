// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IERC20} from "./interfaces/IERC20.sol";
import {IFlashLoanSimpleReceiver, IPool, IPoolAddressesProvider} from "./interfaces/IAaveV3Pool.sol";
import {IUniswapV2Router02} from "./interfaces/IUniswapV2Router02.sol";
import {SafeERC20Lite} from "./libraries/SafeERC20Lite.sol";

/// @title Liquidator
/// @notice Ликвидация проблемных позиций Aave V3 с флеш-кредитом на погашение долга.
/// @dev Экономика: бонус ликвидатора (5–10%) минус комиссия флеш-кредита (0.05%),
///      минус своп залога в долг и минус газ. Контракт обязан вернуть долг в той же
///      транзакции, поэтому залог продаётся сразу после получения.
contract Liquidator is IFlashLoanSimpleReceiver {
    using SafeERC20Lite for IERC20;

    /// @notice Владелец контракта.
    address public owner;
    /// @notice Keeper (горячий кошелёк бота).
    address public keeper;
    /// @notice Провайдер адресов Aave V3.
    IPoolAddressesProvider public immutable addressesProvider;
    /// @notice Контракт на паузе.
    bool public paused;
    /// @notice Успешные ликвидации (метрика для панели).
    uint256 public liquidationCount;
    /// @notice Кумулятивная прибыль в долговом активе.
    mapping(address => uint256) public accumulatedProfit;

    /// @notice Параметры ликвидации.
    struct LiquidationParams {
        address collateralAsset; // залог, который забираем
        address debtAsset;       // долг, который закрываем (его же занимаем)
        address user;            // ликвидируемый заёмщик
        uint256 debtToCover;     // сколько долга закрываем (с учётом close factor)
        address swapRouter;      // маршрутизатор продажи залога
        address[] swapPath;      // путь: collateralAsset -> debtAsset
        uint256 minProfit;       // минимальная прибыль в debtAsset
        uint256 deadline;        // крайний срок (защита от переупаковки блока)
    }

    event LiquidationExecuted(address indexed user, address indexed debtAsset, uint256 debtCovered, uint256 profit);
    event KeeperUpdated(address indexed keeper);
    event PausedSet(bool paused);

    error NotOwner();
    error NotKeeper();
    error ContractPaused();
    error NotAavePool();
    error NotSelfInitiated();
    error Unprofitable(uint256 profit, uint256 minProfit);
    error DeadlineExpired();

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

    /// @param provider PoolAddressesProvider Aave V3 сети.
    /// @param initialKeeper Горячий кошелёк бота.
    constructor(address provider, address initialKeeper) {
        owner = msg.sender;
        addressesProvider = IPoolAddressesProvider(provider);
        keeper = initialKeeper;
        emit KeeperUpdated(initialKeeper);
    }

    /// @notice Назначает keeper-а.
    function setKeeper(address newKeeper) external onlyOwner {
        keeper = newKeeper;
        emit KeeperUpdated(newKeeper);
    }

    /// @notice Пауза/возобновление.
    function setPaused(bool value) external onlyOwner {
        paused = value;
        emit PausedSet(value);
    }

    /// @notice Публичная проверка: стоит ли вообще ликвидировать (health factor < 1).
    /// @dev Keeper вызывает перед подписью, чтобы не жечь газ на «уже здоровой» позиции.
    function isLiquidatable(address user, uint256 thresholdHfWei) external view returns (bool) {
        (,,,,, uint256 healthFactor) = IPool(addressesProvider.getPool()).getUserAccountData(user);
        return healthFactor < thresholdHfWei; // 1e18 == 1.0
    }

    /// @notice Запускает ликвидацию с флеш-кредитом на debtAsset.
    function liquidate(LiquidationParams calldata params) external onlyKeeper whenNotPaused {
        IPool(addressesProvider.getPool()).flashLoanSimple(
            address(this), params.debtAsset, params.debtToCover, abi.encode(params), 0
        );
    }

    /// @notice Колбэк флеш-кредита: погашаем долг, продаём залог, возвращаем займ.
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external override returns (bool) {
        if (msg.sender != addressesProvider.getPool()) revert NotAavePool();
        if (initiator != address(this)) revert NotSelfInitiated();

        LiquidationParams memory liq = abi.decode(params, (LiquidationParams));
        if (liq.deadline != 0 && block.timestamp > liq.deadline) revert DeadlineExpired();

        uint256 startBalance = IERC20(asset).balanceOf(address(this));

        // 1. Ликвидируем позицию: получаем залог, отдавая занятый долг
        IERC20(asset).forceApprove(address(addressesProvider.getPool()), amount);
        IPool(addressesProvider.getPool()).liquidationCall(
            liq.collateralAsset, liq.debtAsset, liq.user, amount, false
        );

        // 2. Продаём залог обратно в долговой актив
        uint256 collateralBalance = IERC20(liq.collateralAsset).balanceOf(address(this));
        IERC20(liq.collateralAsset).forceApprove(liq.swapRouter, collateralBalance);
        IUniswapV2Router02(liq.swapRouter).swapExactTokensForTokens(
            collateralBalance,
            0,
            liq.swapPath,
            address(this),
            liq.deadline == 0 ? block.timestamp : liq.deadline
        );

        // 3. Проверяем экономику и одобряем возврат долга
        uint256 endBalance = IERC20(asset).balanceOf(address(this));
        uint256 owed = amount + premium;
        if (endBalance + startBalance < owed + liq.minProfit) {
            revert Unprofitable(endBalance + startBalance, owed + liq.minProfit);
        }
        uint256 profit = endBalance + startBalance - owed;
        accumulatedProfit[asset] += profit;
        liquidationCount += 1;

        IERC20(asset).forceApprove(msg.sender, owed);
        emit LiquidationExecuted(liq.user, asset, amount, profit);
        return true;
    }

    /// @notice Снимает накопленную прибыль.
    function sweep(address token, address to, uint256 amount) external onlyOwner {
        uint256 available = IERC20(token).balanceOf(address(this));
        uint256 value = amount == 0 || amount > available ? available : amount;
        IERC20(token).safeTransfer(to, value);
        accumulatedProfit[token] = value <= accumulatedProfit[token] ? accumulatedProfit[token] - value : 0;
    }

    /// @notice Спасение посторонних токенов (например, залога, который не удалось продать).
    function rescueToken(address token, address to) external onlyOwner {
        uint256 balance = IERC20(token).balanceOf(address(this));
        if (balance > 0) {
            IERC20(token).safeTransfer(to, balance);
        }
    }

    receive() external payable {}
}
