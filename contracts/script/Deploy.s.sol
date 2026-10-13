// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {Script, console2} from "forge-std/Script.sol";
import {FlashLoanArbitrage} from "../src/FlashLoanArbitrage.sol";
import {Liquidator} from "../src/Liquidator.sol";
import {JITVault} from "../src/JITVault.sol";
import {BridgeAdapter} from "../src/BridgeAdapter.sol";

/// @title Deploy
/// @notice Скрипт деплоя всех контрактов FlashHunter в одну сеть.
/// @dev Адреса Aave V3 читаются из переменных окружения (различаются по сетям):
///      AAVE_POOL_ADDRESSES_PROVIDER, WETH_ADDRESS, KEEPER_ADDRESS.
///
///      Запуск:
///      ```bash
///      forge script script/Deploy.s.sol:Deploy \
///        --rpc-url $ETH_RPC_URL --broadcast --verify -vvvv
///      ```
contract Deploy is Script {
    /// @notice Основной вход: последовательный деплой и запись адресов в лог.
    function run() external returns (address arb, address liq, address jit, address bridge) {
        // Приватный ключ KEEPER-а: в CI/проде приходит из секретов, никогда не коммитится
        uint256 deployerKey = vm.envUint("DEPLOYER_PRIVATE_KEY");
        address keeper = vm.envAddress("KEEPER_ADDRESS");
        address provider = vm.envAddress("AAVE_POOL_ADDRESSES_PROVIDER");
        address weth = vm.envAddress("WETH_ADDRESS");

        vm.startBroadcast(deployerKey);

        FlashLoanArbitrage arbitrage = new FlashLoanArbitrage(provider, weth, keeper);
        Liquidator liquidator = new Liquidator(provider, keeper);
        JITVault jitVault = new JITVault(keeper);
        BridgeAdapter adapter = new BridgeAdapter(keeper);

        vm.stopBroadcast();

        arb = address(arbitrage);
        liq = address(liquidator);
        jit = address(jitVault);
        bridge = address(adapter);

        console2.log("FlashLoanArbitrage:", arb);
        console2.log("Liquidator:", liq);
        console2.log("JITVault:", jit);
        console2.log("BridgeAdapter:", bridge);
    }

    /// @notice Деплой только в dry-run (без broadcast) для проверки в CI.
    function runSimulated() external returns (address arb) {
        address keeper = vm.envOr("KEEPER_ADDRESS", address(0xBEEF));
        address provider = vm.envOr("AAVE_POOL_ADDRESSES_PROVIDER", address(0x2f39d218133AFaB8F2B819B1066c7E434Ad94E9e));
        address weth = vm.envOr("WETH_ADDRESS", address(0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2));

        FlashLoanArbitrage arbitrage = new FlashLoanArbitrage(provider, weth, keeper);
        console2.log("FlashLoanArbitrage (simulated):", address(arbitrage));
        return address(arbitrage);
    }
}
