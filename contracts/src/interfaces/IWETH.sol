// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title IWETH
/// @notice Обёрнутый нативный токен: нужен для работы с парами WETH и возврата газа.
interface IWETH {
    function deposit() external payable;

    function withdraw(uint256 amount) external;

    function transfer(address to, uint256 value) external returns (bool);

    function balanceOf(address account) external view returns (uint256);
}
