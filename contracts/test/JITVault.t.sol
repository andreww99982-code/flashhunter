// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Test} from "forge-std/Test.sol";
import {JITVault} from "../src/JITVault.sol";
import {MockERC20} from "../src/mocks/MockERC20.sol";

/// @title MockV3Pool
/// @notice Мини-пул V3, достаточный для проверки механики JITVault:
///         mint забирает токены через колбэк у вызывающего (pull-модель),
///         burn возвращает внесённое, collect отдаёт «комиссии».
contract MockV3Pool {
    address public immutable token0;
    address public immutable token1;
    uint256 public lastLiquidity;

    constructor(address token0_, address token1_) {
        token0 = token0_;
        token1 = token1_;
    }

    function mint(address, int24, int24, uint128 amount, bytes calldata data) external returns (uint256, uint256) {
        lastLiquidity = amount;
        (address pool, address, address) = abi.decode(data, (address, address, address));
        require(pool == address(this), "bad pool");
        // Pull-модель Uniswap V3: пул просит токены у msg.sender через колбэк
        JITMintCallback(msg.sender).uniswapV3MintCallback(amount, amount, data);
        return (amount, amount);
    }

    function burn(int24, int24, uint128 amount) external returns (uint256 out0, uint256 out1) {
        // Возвращаем внесённое (комиссии приходят отдельно, в collect)
        out0 = amount * 1000;
        out1 = amount * 1000;
        _pay(token0, msg.sender, out0);
        _pay(token1, msg.sender, out1);
    }

    function collect(address recipient, int24, int24, uint128, uint128) external returns (uint128 fees0, uint128 fees1) {
        // «Комиссии» — фиксированная награда за ликвидность во время свопа
        fees0 = 5e15;
        fees1 = 5e15;
        _pay(token0, recipient, fees0);
        _pay(token1, recipient, fees1);
    }

    function _pay(address token, address to, uint256 amount) private {
        (bool ok, bytes memory data) = token.call(abi.encodeWithSignature("transfer(address,uint256)", to, amount));
        require(ok && (data.length == 0 || abi.decode(data, (bool))), "transfer failed");
    }
}

/// @notice Интерфейс колбэка pull-модели Uniswap V3 (без возвращаемого значения).
interface JITMintCallback {
    function uniswapV3MintCallback(uint256 amount0Owed, uint256 amount1Owed, bytes calldata data) external;
}

/// @title JITVaultTest
/// @notice Тесты JIT-контракта: роли, пауза, реентрантность, снятие средств.
contract JITVaultTest is Test {
    JITVault internal vault;
    MockV3Pool internal pool;
    MockERC20 internal token0;
    MockERC20 internal token1;

    address internal keeper = address(0xBEEF);
    address internal attacker = address(0xBAD);

    function setUp() public {
        token0 = new MockERC20("Token0", "TK0", 18);
        token1 = new MockERC20("Token1", "TK1", 18);
        pool = new MockV3Pool(address(token0), address(token1));
        vault = new JITVault(keeper);

        token0.mint(address(vault), 1_000e18);
        token1.mint(address(vault), 1_000e18);
        token0.mint(address(pool), 1_000_000e18);
        token1.mint(address(pool), 1_000_000e18);
    }

    function _params() internal view returns (JITVault.JitParams memory) {
        return JITVault.JitParams({
            pool: address(pool),
            tickLower: -120,
            tickUpper: 120,
            liquidity: 1e18,
            token: address(token0)
        });
    }

    function testMintCallbackOnlyAcceptsItsPool() public {
        vm.expectRevert("only pool");
        vault.uniswapV3MintCallback(1, 1, abi.encode(address(attacker), address(token0), address(token1)));
    }

    function testOnlyKeeperCanRunJit() public {
        vm.prank(attacker);
        vm.expectRevert(JITVault.NotKeeper.selector);
        vault.runJit(_params(), true);
    }

    function testPauseBlocksJit() public {
        vault.setPaused(true);
        vm.prank(keeper);
        vm.expectRevert(JITVault.ContractPaused.selector);
        vault.runJit(_params(), true);
    }

    function testRunJitCollectsFees() public {
        uint256 balance0Before = token0.balanceOf(address(vault));
        uint256 balance1Before = token1.balanceOf(address(vault));

        vm.prank(keeper);
        (uint256 fees0, uint256 fees1) = vault.runJit(_params(), true);

        assertEq(fees0, 5e15);
        assertEq(fees1, 5e15);
        assertGt(token0.balanceOf(address(vault)), balance0Before);
        assertGt(token1.balanceOf(address(vault)), balance1Before);
        assertEq(vault.jitCount(), 1);
    }

    function testDanglingPositionKeepsFunds() public {
        // burnImmediately = false: ликвидность остаётся в пуле (своп ещё не прошёл)
        uint256 balanceBefore = token0.balanceOf(address(vault));
        vm.prank(keeper);
        vault.runJit(_params(), false);
        assertEq(pool.lastLiquidity(), 1e18);
        assertLe(token0.balanceOf(address(vault)), balanceBefore);
    }

    function testSweepOnlyOwner() public {
        vm.prank(attacker);
        vm.expectRevert(JITVault.NotOwner.selector);
        vault.sweep(address(token0), attacker, 0);

        vault.sweep(address(token0), address(this), 0);
        assertEq(token0.balanceOf(address(vault)), 0);
    }

    function testKeeperRotationByOwner() public {
        vault.setKeeper(attacker);
        vm.prank(attacker);
        // Новый keeper (attacker) теперь может запускать JIT
        vault.runJit(_params(), true);
        assertEq(vault.jitCount(), 1);
    }
}
