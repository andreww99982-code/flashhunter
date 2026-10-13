// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {MockERC20} from "./MockERC20.sol";
import {MockV2Pair} from "./MockV2Pair.sol";

/// @title MockV2Router
/// @notice Мини-роутер над MockV2Pair: реализует swapExactTokensForTokens по пути.
///         Эмулирует UniswapV2Router02 ровно настолько, насколько его использует бот.
contract MockV2Router {
    mapping(bytes32 => address) public pairFor; // keccak(tokenA,tokenB) => pair

    /// @notice Регистрирует пару для двух токенов (в обе стороны).
    function registerPair(address tokenA, address tokenB, address pair) external {
        pairFor[keccak256(abi.encode(tokenA, tokenB))] = pair;
        pairFor[keccak256(abi.encode(tokenB, tokenA))] = pair;
    }

    /// @notice Сколько получим на выходе для пути (без комиссий газа).
    function getAmountsOut(uint256 amountIn, address[] calldata path) external view returns (uint256[] memory amounts) {
        amounts = new uint256[](path.length);
        amounts[0] = amountIn;
        for (uint256 index = 0; index + 1 < path.length; ++index) {
            address pair = pairFor[keccak256(abi.encode(path[index], path[index + 1]))];
            require(pair != address(0), "no pair");
            amounts[index + 1] = MockV2Pair(pair).quote(path[index], amounts[index]);
        }
    }

    /// @notice Своп ровно `amountIn` по пути path; средства приходят получателю `to`.
    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        address[] calldata path,
        address to,
        uint256 deadline
    ) external returns (uint256[] memory amounts) {
        require(block.timestamp <= deadline, "expired");
        require(path.length >= 2, "bad path");

        amounts = new uint256[](path.length);
        amounts[0] = amountIn;

        MockERC20(path[0]).transferFrom(msg.sender, address(this), amountIn);
        for (uint256 index = 0; index + 1 < path.length; ++index) {
            address pair = pairFor[keccak256(abi.encode(path[index], path[index + 1]))];
            require(pair != address(0), "no pair");
            MockERC20(path[index]).transfer(pair, amounts[index]);
            amounts[index + 1] = MockV2Pair(pair).swap(path[index], amounts[index]);
        }
        require(amounts[amounts.length - 1] >= amountOutMin, "INSUFFICIENT_OUTPUT_AMOUNT");
        MockERC20(path[path.length - 1]).transfer(to, amounts[amounts.length - 1]);
    }
}
