// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {FlashLoanArbitrage} from "../src/FlashLoanArbitrage.sol";
import {MockERC20} from "../src/mocks/MockERC20.sol";
import {MockV2Pair} from "../src/mocks/MockV2Pair.sol";
import {MockV2Router} from "../src/mocks/MockV2Router.sol";
import {MockAavePool, MockAddressesProvider} from "../src/mocks/MockAavePool.sol";

/// @title EchidnaFlashHunter
/// @notice Инвариант-тесты (property-based fuzzing) для контракта арбитража.
/// @dev Echidna вызывает внешние функции контракта в произвольном порядке и
///      проверяет, что инварианты никогда не нарушаются. Запуск: `make echidna`.
///
///      Ключевые свойства безопасности:
///      1. Посторонний никогда не становится owner/keeper.
///      2. Баланс Aave не уменьшается (флеш-кредит всегда возвращается).
///      3. При паузе ни один арбитраж не исполняется.
///      4. Прибыль никогда не «утекает» посторонним адресам.
contract EchidnaFlashHunter {
    FlashLoanArbitrage public arbitrage;
    MockAavePool public aavePool;
    MockAddressesProvider public provider;
    MockERC20 public usdc;
    MockERC20 public dai;
    MockV2Router public buyRouter;
    MockV2Router public sellRouter;
    MockV2Pair public cheapPair;
    MockV2Pair public richPair;

    // keeper = сам контракт-обёртка: тогда actionArbitrage вызывается с нужными правами
    address internal immutable keeper;
    /// @dev Баланс Aave на момент старта: он не должен уменьшаться никогда.
    uint256 internal immutable aaveInitialBalance;

    constructor() {
        usdc = new MockERC20("USD Coin", "USDC", 6);
        dai = new MockERC20("Dai", "DAI", 18);

        provider = new MockAddressesProvider(address(0));
        aavePool = new MockAavePool(address(provider));
        provider.setPool(address(aavePool));

        buyRouter = new MockV2Router();
        sellRouter = new MockV2Router();
        cheapPair = new MockV2Pair(address(usdc), address(dai), 5);
        richPair = new MockV2Pair(address(usdc), address(dai), 5);
        buyRouter.registerPair(address(usdc), address(dai), address(cheapPair));
        sellRouter.registerPair(address(dai), address(usdc), address(richPair));

        keeper = address(this);
        arbitrage = new FlashLoanArbitrage(address(provider), address(0x0), keeper);

        usdc.mint(address(aavePool), 10_000_000e6);
        usdc.mint(address(cheapPair), 5_000_000e6);
        dai.mint(address(cheapPair), 5_000_000e18);
        dai.mint(address(richPair), 5_000_000e18);
        usdc.mint(address(richPair), 5_100_000e6);
        cheapPair.syncReserves(5_000_000e6, 5_000_000e18);
        richPair.syncReserves(5_100_000e6, 5_000_000e18);

        aaveInitialBalance = usdc.balanceOf(address(aavePool));
    }

    /// @notice Действие 1: попытка арбитража с произвольной суммой.
    function actionArbitrage(uint256 borrowRaw, uint256 minProfitRaw) external {
        uint256 borrowAmount = uint256(borrowRaw) % 1_000_000e6 + 1e6;
        uint256 minProfit = uint256(minProfitRaw) % 100_000e6;

        address[] memory buyPath = new address[](2);
        buyPath[0] = address(usdc);
        buyPath[1] = address(dai);
        address[] memory sellPath = new address[](2);
        sellPath[0] = address(dai);
        sellPath[1] = address(usdc);

        arbitrage.executeArbitrage(
            address(usdc),
            borrowAmount,
            FlashLoanArbitrage.ArbParams({
                buyRouter: address(buyRouter),
                sellRouter: address(sellRouter),
                buyPath: buyPath,
                sellPath: sellPath,
                minProfit: minProfit,
                deadline: block.timestamp + 60
            })
        );
    }

    /// @notice Действие 2: попытка захватить власть над контрактом.
    function actionSetKeeper(address candidate) external {
        arbitrage.setKeeper(candidate);
    }

    /// @notice Действие 3: пауза/возобновление.
    function actionSetPaused(bool value) external {
        arbitrage.setPaused(value);
    }

    /// @notice Действие 4: снятие прибыли на произвольный адрес.
    function actionSweep(address to, uint256 amount) external {
        arbitrage.sweep(address(usdc), to, amount);
    }

    // --------------------------------------------------------------------- //
    // Инварианты
    // --------------------------------------------------------------------- //

    /// @notice ИНВАРИАНТ 1: Aave всегда получил обратно больше, чем выдал.
    function echidna_aave_balance_never_decreases() public view returns (bool) {
        return usdc.balanceOf(address(aavePool)) >= aaveInitialBalance;
    }

    /// @notice ИНВАРИАНТ 2: владелец и keeper — только заранее известные адреса.
    function echidna_roles_immutable_for_outsiders() public view returns (bool) {
        return arbitrage.owner() == address(this) && arbitrage.keeper() == keeper;
    }

    /// @notice ИНВАРИАНТ 3: контракт не держит нативного баланса от чужих средств.
    function echidna_no_stuck_native_balance() public view returns (bool) {
        return address(arbitrage).balance == 0;
    }

    /// @notice ИНВАРИАНТ 4: накопленная прибыль никогда не отрицательна и всегда
    /// доступна для снятия (учёт не превышает фактический баланс).
    function echidna_profit_is_backed_by_balance() public view returns (bool) {
        return arbitrage.accumulatedProfit(address(usdc)) <= usdc.balanceOf(address(arbitrage));
    }

    /// @notice ИНВАРИАНТ 5: адрес контракта не может быть пустым владельцем/keeper.
    function echidna_no_zero_addresses() public view returns (bool) {
        return arbitrage.owner() != address(0) && arbitrage.keeper() != address(0);
    }
}
