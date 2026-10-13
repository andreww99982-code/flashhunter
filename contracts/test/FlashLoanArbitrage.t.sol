// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {FlashLoanArbitrage} from "../src/FlashLoanArbitrage.sol";
import {MockERC20} from "../src/mocks/MockERC20.sol";
import {MockV2Pair} from "../src/mocks/MockV2Pair.sol";
import {MockV2Router} from "../src/mocks/MockV2Router.sol";
import {MockAavePool, MockAddressesProvider} from "../src/mocks/MockAavePool.sol";
import {MockWETH} from "../src/mocks/MockWETH.sol";

/// @title FlashLoanArbitrageTest
/// @notice Тесты контракта арбитража: happy path, защита от убытка, роли, пауза, фаззинг.
/// @dev Модель: два независимых роутера (как два DEX в реальности) с рассинхроном цены.
contract FlashLoanArbitrageTest is Test {
    FlashLoanArbitrage internal arbitrage;
    MockAavePool internal aavePool;
    MockAddressesProvider internal provider;
    MockV2Router internal buyRouter;   // DEX, где USDC дешёвый
    MockV2Router internal sellRouter;  // DEX, где USDC дорогой
    MockV2Pair internal cheapPair;
    MockV2Pair internal richPair;
    MockERC20 internal usdc;
    MockERC20 internal dai;
    MockWETH internal weth;

    address internal keeper = address(0xBEEF);
    address internal attacker = address(0xBAD);

    function setUp() public {
        usdc = new MockERC20("USD Coin", "USDC", 6);
        dai = new MockERC20("Dai", "DAI", 18);
        weth = new MockWETH();

        provider = new MockAddressesProvider(address(0));
        aavePool = new MockAavePool(address(provider));
        provider.setPool(address(aavePool));

        buyRouter = new MockV2Router();
        sellRouter = new MockV2Router();
        cheapPair = new MockV2Pair(address(usdc), address(dai), 5);
        richPair = new MockV2Pair(address(usdc), address(dai), 5);
        buyRouter.registerPair(address(usdc), address(dai), address(cheapPair));
        sellRouter.registerPair(address(dai), address(usdc), address(richPair));

        arbitrage = new FlashLoanArbitrage(address(provider), address(weth), keeper);

        // Фонд флеш-кредита и ликвидность пулов
        usdc.mint(address(aavePool), 10_000_000e6);
        usdc.mint(address(cheapPair), 5_000_000e6);
        dai.mint(address(cheapPair), 5_000_000e18);
        dai.mint(address(richPair), 5_000_000e18);
        usdc.mint(address(richPair), 5_100_000e6); // на этом DEX USDC дороже

        cheapPair.syncReserves(5_000_000e6, 5_000_000e18);
        richPair.syncReserves(5_100_000e6, 5_000_000e18);
    }

    function _params(uint256 minProfit) internal view returns (FlashLoanArbitrage.ArbParams memory) {
        address[] memory buyPath = new address[](2);
        buyPath[0] = address(usdc);
        buyPath[1] = address(dai);
        address[] memory sellPath = new address[](2);
        sellPath[0] = address(dai);
        sellPath[1] = address(usdc);

        return FlashLoanArbitrage.ArbParams({
            buyRouter: address(buyRouter),
            sellRouter: address(sellRouter),
            buyPath: buyPath,
            sellPath: sellPath,
            minProfit: minProfit,
            deadline: block.timestamp + 120
        });
    }

    function testExecuteArbitrageHappyPath() public {
        uint256 borrowAmount = 100_000e6;
        uint256 balanceBefore = usdc.balanceOf(address(arbitrage));

        vm.prank(keeper);
        arbitrage.executeArbitrage(address(usdc), borrowAmount, _params(0));

        uint256 profit = usdc.balanceOf(address(arbitrage)) - balanceBefore;
        assertGt(profit, 0, "arbitrage must be profitable");
        assertEq(arbitrage.accumulatedProfit(address(usdc)), profit);
        // Aave получил долг + комиссию 0.05%
        assertEq(usdc.balanceOf(address(aavePool)), 10_000_000e6 + (borrowAmount * 5) / 10_000);
    }

    function testRevertsWhenUnprofitable() public {
        // Требуем невозможную прибыль: должен сработать guard Unprofitable
        uint256 borrowAmount = 100_000e6;
        FlashLoanArbitrage.ArbParams memory params = _params(1_000_000_000e6);

        vm.prank(keeper);
        vm.expectRevert();
        arbitrage.executeArbitrage(address(usdc), borrowAmount, params);
        // Средства контракта не изменились — транзакция атомарна
        assertEq(usdc.balanceOf(address(arbitrage)), 0);
    }

    function testOnlyKeeperCanExecute() public {
        vm.prank(attacker);
        vm.expectRevert(FlashLoanArbitrage.NotKeeper.selector);
        arbitrage.executeArbitrage(address(usdc), 1e6, _params(0));
    }

    function testPausedBlocksExecution() public {
        arbitrage.setPaused(true);
        vm.prank(keeper);
        vm.expectRevert(FlashLoanArbitrage.ContractPaused.selector);
        arbitrage.executeArbitrage(address(usdc), 1e6, _params(0));
    }

    function testOnlyOwnerCanSetKeeperAndPause() public {
        vm.startPrank(attacker);
        vm.expectRevert(FlashLoanArbitrage.NotOwner.selector);
        arbitrage.setKeeper(attacker);
        vm.expectRevert(FlashLoanArbitrage.NotOwner.selector);
        arbitrage.setPaused(true);
        vm.stopPrank();
    }

    function testCallbackRejectsForeignCaller() public {
        vm.expectRevert(FlashLoanArbitrage.NotAavePool.selector);
        arbitrage.executeOperation(address(usdc), 1e6, 500, address(arbitrage), abi.encode(_params(0)));
    }

    function testCallbackRejectsForeignInitiator() public {
        vm.prank(address(aavePool));
        vm.expectRevert(FlashLoanArbitrage.NotSelfInitiated.selector);
        arbitrage.executeOperation(address(usdc), 1e6, 500, attacker, abi.encode(_params(0)));
    }

    function testExpiredDeadlineReverts() public {
        FlashLoanArbitrage.ArbParams memory params = _params(0);
        params.deadline = block.timestamp - 1;
        vm.prank(keeper);
        vm.expectRevert(FlashLoanArbitrage.DeadlineExpired.selector);
        arbitrage.executeArbitrage(address(usdc), 1e6, params);
    }

    function testSweepTransfersProfitToOwner() public {
        usdc.mint(address(arbitrage), 1_000e6);
        uint256 ownerBefore = usdc.balanceOf(address(this));
        arbitrage.sweep(address(usdc), address(this), 0);
        assertEq(usdc.balanceOf(address(this)), ownerBefore + 1_000e6);
        assertEq(usdc.balanceOf(address(arbitrage)), 0);
    }

    function testUnwrapAndSendNative() public {
        // Кладём WETH контракту и проверяем разгазовку
        weth.deposit{value: 1 ether}();
        weth.transfer(address(arbitrage), 1 ether);
        uint256 ownerBefore = address(this).balance;

        arbitrage.unwrapAndSend(1 ether);
        assertEq(address(this).balance, ownerBefore + 1 ether);
    }

    function testEmergencyCallOnlyOwner() public {
        vm.prank(attacker);
        vm.expectRevert(FlashLoanArbitrage.NotOwner.selector);
        arbitrage.emergencyCall(address(usdc), 0, "");
    }

    function testFuzzArbitrageNeverLosesPrincipal(uint96 borrowRaw, uint32 minProfitRaw) public {
        uint256 borrowAmount = (uint256(borrowRaw) % 1_000_000e6) + 1e6;
        uint256 minProfit = uint256(minProfitRaw) % 500_000e6;

        vm.prank(keeper);
        // Инвариант: либо реверт (защита сработала), либо прибыль >= minProfit
        // и контракт никогда не остаётся должным Aave.
        try arbitrage.executeArbitrage(address(usdc), borrowAmount, _params(minProfit)) {
            assertGe(arbitrage.accumulatedProfit(address(usdc)), minProfit);
            assertGe(usdc.balanceOf(address(aavePool)), 10_000_000e6);
        } catch {}
    }
}
