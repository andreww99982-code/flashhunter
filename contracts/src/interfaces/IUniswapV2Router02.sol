// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title IUniswapV2Router02
/// @notice Интерфейс маршрутизатора V2/Sushi: используется для арбитражных свопов.
interface IUniswapV2Router02 {
    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        address[] calldata path,
        address to,
        uint256 deadline
    ) external returns (uint256[] memory amounts);

    function getAmountsOut(uint256 amountIn, address[] calldata path)
        external
        view
        returns (uint256[] memory amounts);

    function factory() external view returns (address);
}
