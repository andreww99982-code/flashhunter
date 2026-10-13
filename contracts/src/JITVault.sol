// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {IERC20} from "./interfaces/IERC20.sol";
import {IUniswapV3Pool} from "./interfaces/IUniswapV3Pool.sol";
import {SafeERC20Lite} from "./libraries/SafeERC20Lite.sol";

/// @title JITVault
/// @notice Just-In-Time ликвидность в Uniswap V3: ставим узкий диапазон перед
///         крупным свопом и снимаем его сразу после — забирая комиссию свопа.
/// @dev Контракт вызывается keeper-ом в ОДНОЙ транзакции вместе со свопом-жертвой
///      (mint → swap → burn) — иначе позиция останется в пуле и понесёт IL.
/// @dev Политика проекта: контракт НЕ умеет сэндвичить (не умеет покупать до свопа
///      и продавать после) — только предоставление ликвидности.
contract JITVault {
    using SafeERC20Lite for IERC20;

    /// @notice Владелец контракта.
    address public owner;
    /// @notice Keeper (горячий кошелёк бота).
    address public keeper;
    /// @notice Пауза.
    bool public paused;
    /// @notice Заблокирован на время выполнения JIT-цикла (защита от реентранси).
    uint256 private locked = 1;
    /// @notice Счётчик выполненных JIT-циклов.
    uint256 public jitCount;

    /// @notice Параметры JIT-цикла.
    struct JitParams {
        address pool;          // пул V3 (0.05%/0.3% tier и т.п.)
        int24 tickLower;       // нижняя граница узкого диапазона
        int24 tickUpper;       // верхняя граница
        uint128 liquidity;     // размер нашей ликвидности
        address token;         // токен, который выдаём пулу (обычно так, чтобы баланс был 0/0)
    }

    event JitExecuted(address indexed pool, uint128 liquidity, uint256 fees0, uint256 fees1);
    event KeeperUpdated(address indexed keeper);
    event PausedSet(bool paused);

    error NotOwner();
    error NotKeeper();
    error ContractPaused();
    error Reentrancy();
    error NothingCollected();

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    modifier onlyKeeper() {
        if (msg.sender != keeper && msg.sender != owner) revert NotKeeper();
        _;
    }

    modifier whenNotPaused() {
        if (paused) revert ContractPaused();
        _;
    }

    modifier nonReentrant() {
        if (locked != 1) revert Reentrancy();
        locked = 2;
        _;
        locked = 1;
    }

    constructor(address initialKeeper) {
        owner = msg.sender;
        keeper = initialKeeper;
        emit KeeperUpdated(initialKeeper);
    }

    /// @notice Назначает keeper-а.
    function setKeeper(address newKeeper) external onlyOwner {
        keeper = newKeeper;
        emit KeeperUpdated(newKeeper);
    }

    /// @notice Пауза/возобновление.
    function setPaused(bool value) external onlyOwner {
        paused = value;
        emit PausedSet(value);
    }

    /// @notice Колбэк пула V3 при mint: пул запрашивает токены.
    /// @dev Возвращаем true, потому что токены уже переведены до вызова mint
    ///      (pay-then-mint), иначе пришлось бы делать transferFrom, что дороже по газу.
    function uniswapV3MintCallback(uint256 amount0Owed, uint256 amount1Owed, bytes calldata data) external {
        // data = abi.encode(pool, token0, token1)
        (address pool, address token0, address token1) = abi.decode(data, (address, address, address));
        require(msg.sender == pool, "only pool");
        if (amount0Owed > 0) IERC20(token0).safeTransfer(msg.sender, amount0Owed);
        if (amount1Owed > 0) IERC20(token1).safeTransfer(msg.sender, amount1Owed);
    }

    /// @notice Колбэк пула V3 при swap: JIT-контракт не обязан что-либо доплачивать,
    ///         но может использовать колбэк для атомарного снятия ликвидности после свопа.
    function uniswapV3SwapCallback(int256 amount0Delta, int256 amount1Delta, bytes calldata data) external {
        (address pool, address token0, address token1) = abi.decode(data, (address, address, address));
        require(msg.sender == pool, "only pool");
        if (amount0Delta > 0) IERC20(token0).safeTransfer(msg.sender, uint256(amount0Delta));
        if (amount1Delta > 0) IERC20(token1).safeTransfer(msg.sender, uint256(amount1Delta));
    }

    /// @notice Выполняет полный JIT-цикл: mint → (своп уже сделан снаружи) → burn → collect.
    /// @dev Вызывается keeper-ом в мультиколле с транзакцией свопа, поэтому
    ///      в реальном сценарии своп идёт между mint и burn через отдельную atomic-обёртку.
    function runJit(JitParams calldata params, bool burnImmediately)
        external
        onlyKeeper
        whenNotPaused
        nonReentrant
        returns (uint256 fees0, uint256 fees1)
    {
        IUniswapV3Pool pool = IUniswapV3Pool(params.pool);
        address token0 = pool.token0();
        address token1 = pool.token1();

        uint256 balance0Before = IERC20(token0).balanceOf(address(this));
        uint256 balance1Before = IERC20(token1).balanceOf(address(this));

        pool.mint(address(this), params.tickLower, params.tickUpper, params.liquidity, abi.encode(params.pool, token0, token1));

        if (burnImmediately) {
            // Снимаем позицию: пул вернёт токены, комиссии останутся доступными к collect
            pool.burn(params.tickLower, params.tickUpper, params.liquidity);
        }
        (fees0, fees1) = pool.collect(
            address(this), params.tickLower, params.tickUpper, type(uint128).max, type(uint128).max
        );

        uint256 balance0After = IERC20(token0).balanceOf(address(this));
        uint256 balance1After = IERC20(token1).balanceOf(address(this));

        // Инвариант JIT: мы не должны потерять больше, чем заработали на комиссиях
        // (кроме случая, когда своп ещё не прошёл — тогда позиция остаётся в пуле).
        if (burnImmediately && balance0After < balance0Before + fees0 && balance1After < balance1Before + fees1) {
            revert NothingCollected();
        }
        jitCount += 1;
        emit JitExecuted(params.pool, params.liquidity, fees0, fees1);
    }

    /// @notice Снимает накопленные токены (прибыль JIT-циклов) на адрес получателя.
    function sweep(address token, address to, uint256 amount) external onlyOwner {
        uint256 available = IERC20(token).balanceOf(address(this));
        uint256 value = amount == 0 || amount > available ? available : amount;
        IERC20(token).safeTransfer(to, value);
    }

    /// @notice Аварийная передача владения.
    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "zero owner");
        owner = newOwner;
    }

    receive() external payable {}
}
