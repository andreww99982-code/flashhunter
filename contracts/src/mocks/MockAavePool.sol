// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {MockERC20} from "./MockERC20.sol";
import {IFlashLoanSimpleReceiver, IPool, IPoolAddressesProvider} from "../interfaces/IAaveV3Pool.sol";

/// @title MockAavePool
/// @notice Эмуляция Aave V3: flashLoanSimple выдаёт средства, вызывает колбэк
///         и списывает amount + premium. Используется для тестов и для прогонов
///         на форке без реальных денег.
contract MockAavePool is IPool {
    /// @notice Комиссия флеш-кредита в bps (5 = 0.05%, как в Aave V3).
    uint16 public constant FLASH_FEE_BPS = 5;
    /// @notice Кто может инициировать ликвидацию в моке (проверка вызова контрактом).
    address public immutable addressesProviderLike;

    event FlashLoan(address indexed receiver, address indexed asset, uint256 amount, uint256 premium);
    event Liquidation(address indexed collateral, address indexed debt, address indexed user, uint256 covered);

    constructor(address providerLike) {
        addressesProviderLike = providerLike;
    }

    /// @notice Простой флеш-кредит одного актива (как в Aave V3).
    function flashLoanSimple(
        address receiverAddress,
        address asset,
        uint256 amount,
        bytes calldata params,
        uint16 /* referralCode */
    ) external {
        uint256 balanceBefore = MockERC20(asset).balanceOf(address(this));
        MockERC20(asset).transfer(receiverAddress, amount);
        uint256 premium = (amount * FLASH_FEE_BPS) / 10_000;

        require(
            IFlashLoanSimpleReceiver(receiverAddress).executeOperation(asset, amount, premium, receiverAddress, params),
            "callback failed"
        );

        // Пул должен получить обратно amount + premium (Aave списывает по allowance)
        MockERC20(asset).transferFrom(receiverAddress, address(this), amount + premium);
        require(MockERC20(asset).balanceOf(address(this)) >= balanceBefore + premium, "premium not paid");
        emit FlashLoan(receiverAddress, asset, amount, premium);
    }

    /// @notice Упрощённая ликвидация: забираем debtToCover, отдаём залог 1:1 * бонус 5%.
    function liquidationCall(
        address collateralAsset,
        address debtAsset,
        address user,
        uint256 debtToCover,
        bool /* receiveAToken */
    ) external {
        // В моке: списываем долг у инициатора, отдаём залог c бонусом 5%
        MockERC20(debtAsset).transferFrom(msg.sender, address(this), debtToCover);
        uint256 collateralGiven = (debtToCover * 105) / 100;
        uint256 available = MockERC20(collateralAsset).balanceOf(address(this));
        if (available < collateralGiven) {
            collateralGiven = available; // ограничение ликвидности мока
        }
        MockERC20(collateralAsset).transfer(msg.sender, collateralGiven);
        emit Liquidation(collateralAsset, debtAsset, user, debtToCover);
    }

    /// @notice Пустая реализация: health factor в моке не моделируется точно.
    function getUserAccountData(address /* user */)
        external
        pure
        returns (uint256, uint256, uint256, uint256, uint256, uint256)
    {
        return (0, 0, 0, 0, 0, 0.95e18);
    }
}

/// @title MockAddressesProvider
/// @notice Провайдер адресов Aave V3 для тестов: отдаёт заранее заданный пул.
contract MockAddressesProvider is IPoolAddressesProvider {
    address public pool;

    constructor(address pool_) {
        pool = pool_;
    }

    function setPool(address pool_) external {
        pool = pool_;
    }

    function getPool() external view returns (address) {
        return pool;
    }
}
