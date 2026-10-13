// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {MockERC20} from "./MockERC20.sol";

/// @title MockV2Pair
/// @notice Упрощённая V2-пара (x*y=k) для тестов арбитража: позволяет
///         воспроизводить рассинхрон цен между двумя «пулами».
contract MockV2Pair {
    MockERC20 public immutable token0;
    MockERC20 public immutable token1;
    uint112 public reserve0;
    uint112 public reserve1;
    uint16 public immutable feeBps;

    constructor(address token0_, address token1_, uint16 feeBps_) {
        token0 = MockERC20(token0_);
        token1 = MockERC20(token1_);
        feeBps = feeBps_;
    }

    /// @notice Синхронизация резервов (эмуляция внешнего свопа).
    function syncReserves(uint112 r0, uint112 r1) external {
        reserve0 = r0;
        reserve1 = r1;
    }

    /// @notice Своп с комиссией: стандартная формула x*y=k с учётом fee.
    function swap(address tokenIn, uint256 amountIn) external returns (uint256 amountOut) {
        bool zeroForOne = tokenIn == address(token0);
        (uint112 rIn, uint112 rOut) = zeroForOne ? (reserve0, reserve1) : (reserve1, reserve0);
        uint256 amountInAfterFee = amountIn * (10_000 - feeBps);
        amountOut = (amountInAfterFee * rOut) / (rIn * 10_000 + amountInAfterFee);

        MockERC20(tokenIn).transferFrom(msg.sender, address(this), amountIn);
        zeroForOne ? MockERC20(address(token1)).transfer(msg.sender, amountOut) : MockERC20(address(token0)).transfer(msg.sender, amountOut);

        if (zeroForOne) {
            reserve0 = rIn + uint112(amountIn);
            reserve1 = rOut - uint112(amountOut);
        } else {
            reserve1 = rIn + uint112(amountIn);
            reserve0 = rOut - uint112(amountOut);
        }
    }

    /// @notice Просмотр суммы выхода без изменения состояния (как getAmountsOut).
    function quote(address tokenIn, uint256 amountIn) external view returns (uint256) {
        bool zeroForOne = tokenIn == address(token0);
        (uint112 rIn, uint112 rOut) = zeroForOne ? (reserve0, reserve1) : (reserve1, reserve0);
        uint256 amountInAfterFee = amountIn * (10_000 - feeBps);
        return (amountInAfterFee * rOut) / (rIn * 10_000 + amountInAfterFee);
    }
}
