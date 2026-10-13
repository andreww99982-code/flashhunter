// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {BridgeAdapter} from "../src/BridgeAdapter.sol";
import {MockERC20} from "../src/mocks/MockERC20.sol";

/// @title BridgeAdapterTest
/// @notice Тесты адаптера интентов: создание, исполнение, отмена, лимит комиссии.
contract BridgeAdapterTest is Test {
    BridgeAdapter internal adapter;
    MockERC20 internal token;

    address internal owner = address(this);
    address internal solver = address(0xBEEF);
    address internal user = address(0x5017);
    address internal attacker = address(0xBAD);

    function setUp() public {
        adapter = new BridgeAdapter(solver);
        token = new MockERC20("USD Coin", "USDC", 6);
        token.mint(user, 1_000_000e6);
    }

    function _create(uint256 amount, uint16 feeBps, uint256 minOut) internal returns (bytes32 id) {
        vm.startPrank(user);
        token.approve(address(adapter), amount);
        id = adapter.createIntent(
            address(token),
            amount,
            minOut,
            8453,          // destChain: Base
            user,          // получатель в целевой сети
            uint64(block.timestamp + 600),
            feeBps,
            1              // nonce
        );
        vm.stopPrank();
    }

    function testCreateIntentLocksFunds() public {
        bytes32 id = _create(10_000e6, 10, 9_900e6);
        assertEq(token.balanceOf(address(adapter)), 10_000e6);
        assertEq(token.balanceOf(user), 990_000e6);

        (
            address intentUser,
            address tokenIn,
            uint256 amountIn,
            ,
            uint64 destChainId,
            ,
            ,
            uint16 feeBps,
            BridgeAdapter.IntentStatus status
        ) = adapter.intents(id);
        assertEq(intentUser, user);
        assertEq(tokenIn, address(token));
        assertEq(amountIn, 10_000e6);
        assertEq(destChainId, 8453);
        assertEq(feeBps, 10);
        assertEq(uint8(status), uint8(BridgeAdapter.IntentStatus.Open));
    }

    function testFillIntentPaysSolverMinusFee() public {
        bytes32 id = _create(10_000e6, 20, 9_900e6);
        vm.prank(solver);
        adapter.fillIntent(id);
        // Солвер получает amount - fee (20 bps), комиссия остаётся в контракте
        assertEq(token.balanceOf(solver), 10_000e6 - 20e6);
        assertEq(adapter.accumulatedFees(address(token)), 20e6);
    }

    function testOnlyKeeperCanFill() public {
        bytes32 id = _create(1_000e6, 10, 990e6);
        vm.prank(attacker);
        vm.expectRevert(BridgeAdapter.NotKeeper.selector);
        adapter.fillIntent(id);
    }

    function testFeeAboveMaximumRejected() public {
        vm.startPrank(user);
        token.approve(address(adapter), 1_000e6);
        vm.expectRevert(abi.encodeWithSelector(BridgeAdapter.FeeTooHigh.selector, uint16(26), uint16(25)));
        adapter.createIntent(address(token), 1_000e6, 990e6, 8453, user, uint64(block.timestamp + 600), 26, 2);
        vm.stopPrank();
    }

    function testExpiredIntentRejectedOnCreateAndFill() public {
        vm.startPrank(user);
        token.approve(address(adapter), 1_000e6);
        vm.expectRevert(BridgeAdapter.IntentExpired.selector);
        adapter.createIntent(address(token), 1_000e6, 990e6, 8453, user, uint64(block.timestamp - 1), 10, 3);
        vm.stopPrank();

        bytes32 id = _create(1_000e6, 10, 990e6);
        vm.warp(block.timestamp + 601);
        vm.prank(solver);
        vm.expectRevert(BridgeAdapter.IntentExpired.selector);
        adapter.fillIntent(id);
    }

    function testUserCanCancelIntent() public {
        bytes32 id = _create(1_000e6, 10, 990e6);
        uint256 userBalanceBefore = token.balanceOf(user);
        vm.prank(user);
        adapter.cancelIntent(id);
        assertEq(token.balanceOf(user), userBalanceBefore + 1_000e6);
        assertEq(token.balanceOf(address(adapter)), 0);
    }

    function testCannotFillTwice() public {
        bytes32 id = _create(1_000e6, 10, 990e6);
        vm.prank(solver);
        adapter.fillIntent(id);
        vm.prank(solver);
        vm.expectRevert(BridgeAdapter.IntentNotOpen.selector);
        adapter.fillIntent(id);
    }

    function testSweepFees() public {
        bytes32 id = _create(10_000e6, 25, 9_900e6);
        vm.prank(solver);
        adapter.fillIntent(id);
        uint256 fee = adapter.accumulatedFees(address(token));
        adapter.sweepFees(address(token), owner, 0);
        assertEq(token.balanceOf(owner), fee);
        assertEq(adapter.accumulatedFees(address(token)), 0);
    }

    function testFuzzIntentIdIsDeterministic(uint96 amount) public {
        uint256 amountIn = uint256(amount) % 1_000_000e6 + 1e6;
        bytes32 first = adapter.intentId(user, address(token), amountIn, 8453, 1_700_000_000, 7);
        bytes32 second = adapter.intentId(user, address(token), amountIn, 8453, 1_700_000_000, 7);
        bytes32 different = adapter.intentId(user, address(token), amountIn, 8453, 1_700_000_000, 8);
        assertEq(first, second);
        assertTrue(first != different);
    }
}
