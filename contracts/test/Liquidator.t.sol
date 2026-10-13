// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {Liquidator} from "../src/Liquidator.sol";
import {MockERC20} from "../src/mocks/MockERC20.sol";
import {MockV2Pair} from "../src/mocks/MockV2Pair.sol";
import {MockV2Router} from "../src/mocks/MockV2Router.sol";
import {MockAavePool, MockAddressesProvider} from "../src/mocks/MockAavePool.sol";

/// @title LiquidatorTest
/// @notice Тесты ликвидатора: экономика, роли, пауза, порог health factor.
contract LiquidatorTest is Test {
    Liquidator internal liquidator;
    MockAavePool internal aavePool;
    MockAddressesProvider internal provider;
    MockV2Router internal router;
    MockV2Pair internal pair;
    MockERC20 internal collateral; // залог
    MockERC20 internal debt;       // долг

    address internal keeper = address(0xBEEF);
    address internal attacker = address(0xBAD);
    address internal victim = address(0x1CE);

    function setUp() public {
        collateral = new MockERC20("Wrapped BTC", "WBTC", 8);
        debt = new MockERC20("USD Coin", "USDC", 6);

        provider = new MockAddressesProvider(address(0));
        aavePool = new MockAavePool(address(provider));
        provider.setPool(address(aavePool));

        router = new MockV2Router();
        pair = new MockV2Pair(address(collateral), address(debt), 5);
        router.registerPair(address(collateral), address(debt), address(pair));

        liquidator = new Liquidator(address(provider), keeper);

        // Ликвидность: Aave держит долг для флеш-кредита и залог для выдачи
        debt.mint(address(aavePool), 5_000_000e6);
        collateral.mint(address(aavePool), 500e8);
        collateral.mint(address(pair), 100e8);
        debt.mint(address(pair), 3_000_000e6);
        pair.syncReserves(100e8, 3_000_000e6);
    }

    function _params(uint256 debtToCover, uint256 minProfit) internal view returns (Liquidator.LiquidationParams memory) {
        address[] memory path = new address[](2);
        path[0] = address(collateral);
        path[1] = address(debt);
        return Liquidator.LiquidationParams({
            collateralAsset: address(collateral),
            debtAsset: address(debt),
            user: victim,
            debtToCover: debtToCover,
            swapRouter: address(router),
            swapPath: path,
            minProfit: minProfit,
            deadline: block.timestamp + 120
        });
    }

    function testLiquidationHappyPath() public {
        vm.prank(keeper);
        liquidator.liquidate(_params(50_000e6, 0));
        assertEq(liquidator.liquidationCount(), 1);
        // Прибыль = бонус 5% от Aave минус комиссия флеш-кредита 0.05% минус потери свопа
        assertGt(liquidator.accumulatedProfit(address(debt)), 0, "liquidation must be profitable");
    }

    function testUnprofitableLiquidationReverts() public {
        vm.prank(keeper);
        vm.expectRevert();
        liquidator.liquidate(_params(50_000e6, 1_000_000e6));
        assertEq(liquidator.liquidationCount(), 0);
    }

    function testOnlyKeeperCanLiquidate() public {
        vm.prank(attacker);
        vm.expectRevert(Liquidator.NotKeeper.selector);
        liquidator.liquidate(_params(1_000e6, 0));
    }

    function testPauseBlocksLiquidation() public {
        liquidator.setPaused(true);
        vm.prank(keeper);
        vm.expectRevert(Liquidator.ContractPaused.selector);
        liquidator.liquidate(_params(1_000e6, 0));
    }

    function testIsLiquidatableUsesThreshold() public {
        // Мок отдаёт healthFactor = 0.95e18 (позиция нездорова)
        assertTrue(liquidator.isLiquidatable(victim, 1e18));
        assertFalse(liquidator.isLiquidatable(victim, 0.9e18));
    }

    function testSweepOnlyOwner() public {
        debt.mint(address(liquidator), 777e6);
        vm.prank(attacker);
        vm.expectRevert(Liquidator.NotOwner.selector);
        liquidator.sweep(address(debt), attacker, 0);

        liquidator.sweep(address(debt), address(this), 0);
        assertEq(debt.balanceOf(address(liquidator)), 0);
    }

    function testFuzzLiquidationProfitBound(uint96 amountRaw) public {
        uint256 amount = (uint256(amountRaw) % 100_000e6) + 1_000e6;
        vm.prank(keeper);
        try liquidator.liquidate(_params(amount, 0)) {
            // Инвариант: успешная ликвидация всегда оставляет контракт в плюсе
            assertGt(liquidator.accumulatedProfit(address(debt)), 0);
        } catch {}
    }
}
