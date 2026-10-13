// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IERC20} from "../interfaces/IERC20.sol";

/// @title SafeERC20Lite
/// @notice Безопасные обёртки над ERC-20: часть токенов (USDT и др.) не возвращает bool.
/// @dev Библиотека намеренно маленькая: каждая строка — размер контракта и газ.
library SafeERC20Lite {
    error TransferFailed();
    error ApproveFailed();

    /// @notice Безопасный approve с обнулением текущего разрешения (USDT-совместимость).
    function forceApprove(IERC20 token, address spender, uint256 amount) internal {
        // Некоторые токены требуют нулевого allowance перед новым approve
        (bool ok, bytes memory data) = address(token).call(abi.encodeCall(IERC20.approve, (spender, 0)));
        if (!ok || (data.length != 0 && !abi.decode(data, (bool)))) {
            revert ApproveFailed();
        }
        (ok, data) = address(token).call(abi.encodeCall(IERC20.approve, (spender, amount)));
        if (!ok || (data.length != 0 && !abi.decode(data, (bool)))) {
            revert ApproveFailed();
        }
    }

    /// @notice Безопасный transferFrom для блокировки средств в контракте.
    function safeTransferFrom(IERC20 token, address from, address to, uint256 amount) internal {
        (bool ok, bytes memory data) = address(token).call(abi.encodeCall(IERC20.transferFrom, (from, to, amount)));
        if (!ok || (data.length != 0 && !abi.decode(data, (bool)))) {
            revert TransferFailed();
        }
    }

    /// @notice Безопасный transfer, устойчивый к токенам без возвращаемого значения.
    function safeTransfer(IERC20 token, address to, uint256 amount) internal {
        (bool ok, bytes memory data) = address(token).call(abi.encodeCall(IERC20.transfer, (to, amount)));
        if (!ok || (data.length != 0 && !abi.decode(data, (bool)))) {
            revert TransferFailed();
        }
    }
}
