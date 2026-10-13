// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// @title IFlashLoanSimpleReceiver
/// @notice Колбэк Aave V3 для простого флеш-кредита одного актива.
interface IFlashLoanSimpleReceiver {
    /// @param asset Адрес заимствованного актива.
    /// @param amount Сумма займа (raw).
    /// @param premium Комиссия флеш-кредита (0.05% по умолчанию).
    /// @param initiator Адрес инициатора (наш контракт).
    /// @param params Полезная нагрузка (маршрут арбитража).
    /// @return encoded true, если колбэк обработан успешно.
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external returns (bool encoded);
}

/// @title IPool (Aave V3)
/// @notice Часть интерфейса пула Aave V3, используемая проектом.
interface IPool {
    function flashLoanSimple(
        address receiverAddress,
        address asset,
        uint256 amount,
        bytes calldata params,
        uint16 referralCode
    ) external;

    function liquidationCall(
        address collateralAsset,
        address debtAsset,
        address user,
        uint256 debtToCover,
        bool receiveAToken
    ) external;

    function getUserAccountData(address user)
        external
        view
        returns (
            uint256 totalCollateralBase,
            uint256 totalDebtBase,
            uint256 availableBorrowsBase,
            uint256 currentLiquidationThreshold,
            uint256 ltv,
            uint256 healthFactor
        );
}

/// @title IPoolAddressesProvider
/// @notice Провайдер адресов Aave V3: позволяет не хардкодить адрес пула.
interface IPoolAddressesProvider {
    function getPool() external view returns (address);
}
