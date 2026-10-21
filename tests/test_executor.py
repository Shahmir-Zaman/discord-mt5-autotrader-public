import sys
from unittest.mock import MagicMock, patch

mock_mt5 = MagicMock()
mock_mt5.TRADE_RETCODE_DONE = 10009
mock_mt5.ORDER_TYPE_BUY = 0
mock_mt5.ORDER_TYPE_SELL = 1
mock_mt5.ORDER_TYPE_BUY_LIMIT = 2
mock_mt5.ORDER_TYPE_SELL_LIMIT = 3
mock_mt5.ORDER_TYPE_BUY_STOP = 4
mock_mt5.ORDER_TYPE_SELL_STOP = 5
mock_mt5.TRADE_ACTION_DEAL = 1
mock_mt5.TRADE_ACTION_PENDING = 5
mock_mt5.ORDER_TIME_GTC = 0
mock_mt5.ORDER_FILLING_FOK = 1
mock_mt5.ORDER_FILLING_IOC = 2
mock_mt5.ORDER_FILLING_RETURN = 3
mock_mt5.SYMBOL_FILLING_FOK = 1
mock_mt5.SYMBOL_FILLING_IOC = 2
mock_mt5.initialize.return_value = True
sys.modules['MetaTrader5'] = mock_mt5

from src.executor import MT5Executor
from src.parser import SignalType
import pytest

@pytest.fixture(autouse=True)
def reset_mt5_mock_state():
    mock_mt5.positions_get.return_value = []
    mock_mt5.orders_get.return_value = []
    mock_mt5.order_send.side_effect = None
    mock_mt5.order_send.return_value = MagicMock(retcode=10009, order=77777)
    mock_mt5.symbol_select.side_effect = None
    mock_mt5.symbol_select.return_value = True
    yield
    mock_mt5.positions_get.return_value = []
    mock_mt5.orders_get.return_value = []
    mock_mt5.order_send.side_effect = None
    mock_mt5.order_send.return_value = MagicMock(retcode=10009, order=77777)
    mock_mt5.symbol_select.side_effect = None
    mock_mt5.symbol_select.return_value = True

def test_cmp_check_allows_trade_within_threshold():
    """Trade should execute when live price is within 50 pips of signal CMP."""
    executor = MT5Executor(pip_thresholds={"XAUUSD": 50})

    # Signal CMP = 4625.9, live ask = 4628.0 → 21 pips away (within 50)
    mock_tick = MagicMock()
    mock_tick.ask = 4628.0
    mock_mt5.symbol_select.return_value = True
    mock_mt5.symbol_info_tick.return_value = mock_tick
    mock_mt5.symbol_info.return_value = MagicMock(filling_mode=1)

    mock_result = MagicMock()
    mock_result.retcode = 10009
    mock_result.order = 12345
    mock_mt5.order_send.return_value = mock_result

    signal = {"type": SignalType.ENTRY, "action": "BUY", "symbol": "XAUUSD", "price": 4625.9}
    result = executor.execute_signal(signal, channel="provider_a", config={"PROVIDER_A_FIXED_LOT": "0.01"})
    assert "Trade opened" in result
    assert "12345" in result


def test_cmp_check_rejects_trade_outside_threshold():
    """Trade should be rejected when live price exceeds 50 pips from signal CMP."""
    executor = MT5Executor(pip_thresholds={"XAUUSD": 50})

    # Signal CMP = 4625.9, live ask = 4640.0 → 141 pips away (exceeds 50)
    mock_tick = MagicMock()
    mock_tick.ask = 4640.0
    mock_mt5.symbol_select.return_value = True
    mock_mt5.symbol_info_tick.return_value = mock_tick

    signal = {"type": SignalType.ENTRY, "action": "BUY", "symbol": "XAUUSD", "price": 4625.9}
    result = executor.execute_signal(signal, channel="provider_a", config={"PROVIDER_A_FIXED_LOT": "0.01"})
    assert "Trade rejected" in result
    assert "141.0 pips" in result
    assert "max allowed: 50 pips" in result


def test_cmp_check_skipped_for_unconfigured_symbols():
    """Symbols without a pip_threshold should skip the check and execute."""
    executor = MT5Executor(pip_thresholds={"XAUUSD": 50})  # EURUSD not configured

    mock_tick = MagicMock()
    mock_tick.bid = 1.2000  # Far from signal CMP
    mock_mt5.symbol_select.return_value = True
    mock_mt5.symbol_info_tick.return_value = mock_tick
    mock_mt5.symbol_info.return_value = MagicMock(filling_mode=1)

    mock_result = MagicMock()
    mock_result.retcode = 10009
    mock_result.order = 99999
    mock_mt5.order_send.return_value = mock_result

    signal = {"type": SignalType.ENTRY, "action": "SELL", "symbol": "EURUSD", "price": 1.0850}
    result = executor.execute_signal(signal, channel="provider_a", config={"PROVIDER_A_FIXED_LOT": "0.01"})
    # Should execute despite being far from CMP, because EURUSD has no threshold
    assert "Trade opened" in result

def test_open_trade_includes_initial_sl_tp():
    """Trade should include sl and tp in request when provided."""
    executor = MT5Executor(pip_thresholds={"XAUUSD": 50})

    mock_tick = MagicMock()
    mock_tick.ask = 4526.5
    mock_mt5.symbol_select.return_value = True
    mock_mt5.symbol_info_tick.return_value = mock_tick
    mock_mt5.symbol_info.return_value = MagicMock(filling_mode=1)

    mock_result = MagicMock()
    mock_result.retcode = 10009
    mock_result.order = 55555
    mock_mt5.order_send.return_value = mock_result

    signal = {
        "type": SignalType.ENTRY,
        "action": "BUY",
        "symbol": "XAUUSD",
        "price": 4526.5,
        "sl": 4520.0,
        "tp": 4540.0
    }
    config = {"PROVIDER_A_LOT_MODE": "FIXED", "PROVIDER_A_FIXED_LOT": "0.02"}
    result = executor.execute_signal(signal, channel="provider_a", config=config)
    assert "Trade opened" in result
    assert "SL: 4520.0" in result
    assert "TP: 4540.0" in result
    
    # Verify request payload
    call_args = mock_mt5.order_send.call_args[0][0]
    assert call_args["sl"] == 4520.0
    assert call_args["tp"] == 4540.0
    assert call_args["volume"] == 0.02

def test_close_position_success():
    """Close position should send opposite market order to close open trade."""
    executor = MT5Executor()

    mock_pos = MagicMock()
    mock_pos.ticket = 77777
    mock_pos.magic = 123456
    mock_pos.type = 0  # BUY
    mock_pos.volume = 0.01
    mock_mt5.positions_get.return_value = [mock_pos]

    mock_tick = MagicMock()
    mock_tick.bid = 4533.2
    mock_mt5.symbol_info_tick.return_value = mock_tick
    mock_mt5.symbol_info.return_value = MagicMock(filling_mode=1)

    mock_result = MagicMock()
    mock_result.retcode = 10009
    mock_mt5.order_send.return_value = mock_result

    signal = {"type": SignalType.CLOSE_POSITION, "symbol": "XAUUSD"}
    result = executor.execute_signal(signal)
    assert "Position closed" in result
    assert "4533.2" in result

def test_close_multiple_positions_success():
    """Closing a symbol with multiple open positions should close ALL of them."""
    executor = MT5Executor()

    pos1 = MagicMock(ticket=101, magic=123456, type=0, volume=0.01, symbol="XAUUSD")
    pos2 = MagicMock(ticket=102, magic=123456, type=0, volume=0.02, symbol="XAUUSD")
    mock_mt5.positions_get.return_value = [pos1, pos2]

    mock_tick = MagicMock(bid=4535.0)
    mock_mt5.symbol_info_tick.return_value = mock_tick
    mock_mt5.symbol_info.return_value = MagicMock(filling_mode=1)
    mock_mt5.order_send.reset_mock()
    mock_mt5.order_send.return_value = MagicMock(retcode=10009)

    signal = {"type": SignalType.CLOSE_POSITION, "symbol": "XAUUSD"}
    result = executor.execute_signal(signal)
    assert "Position closed" in result
    assert "101" in result
    assert "102" in result
    assert mock_mt5.order_send.call_count == 2

    # Check request payload sent to mt5



def test_calculate_lot_size():
    executor = MT5Executor()
    mock_mt5.account_info.return_value = MagicMock(balance=1000.0)
    # Gold tick value = $1 for 1 lot (example), tick size = 0.01
    # SL is 10 pips away (100 ticks). 100 * 1 = $100 loss per lot.
    # Risk 1% of $1000 = $10.
    # $10 / $100 = 0.1 lot.
    mock_mt5.symbol_info.return_value = MagicMock(trade_tick_size=0.01, trade_tick_value=1.0, volume_step=0.01, volume_min=0.01, volume_max=100.0)
    
    lot = executor._calculate_lot_size('XAUUSD', 4500.0, 4499.0, 1.0, 0.01)
    assert lot == 0.1

def test_calculate_lot_size_clamps_to_min():
    executor = MT5Executor()
    mock_mt5.account_info.return_value = MagicMock(balance=100.0)
    mock_mt5.symbol_info.return_value = MagicMock(trade_tick_size=0.01, trade_tick_value=1.0, volume_step=0.01, volume_min=0.01, volume_max=100.0)
    
    # SL is very far, risk lot would be 0.001. Clamps to 0.01
    lot = executor._calculate_lot_size('XAUUSD', 4500.0, 4400.0, 1.0, 0.01)
    assert lot == 0.01


def test_open_pending_limit_order():
    executor = MT5Executor()
    
    mock_tick = MagicMock()
    mock_tick.ask = 4526.5
    mock_mt5.symbol_select.return_value = True
    mock_mt5.symbol_info_tick.return_value = mock_tick
    mock_mt5.symbol_info.return_value = MagicMock(filling_mode=1)
    
    mock_result = MagicMock()
    mock_result.retcode = 10009
    mock_result.order = 88888
    mock_mt5.order_send.return_value = mock_result
    
    signal = {
        "type": SignalType.ENTRY,
        "action": "SELL",
        "symbol": "XAUUSD",
        "order_type": "LIMIT",
        "price": 4659.7,
        "sl": 4665.5
    }
    
    result = executor.execute_signal(signal, channel="provider_a", config={"PROVIDER_A_FIXED_LOT": "0.05"})
    assert "Trade opened" in result
    
    call_args = mock_mt5.order_send.call_args[0][0]
    assert call_args["action"] == mock_mt5.TRADE_ACTION_PENDING

def test_symbol_suffix_resolution():
    executor = MT5Executor(symbol_suffix="m")
    
    # Mock symbol_select: raw XAUUSD returns False, XAUUSDm returns True
    def mock_symbol_select(sym, enable):
        return sym == "XAUUSDm"
    mock_mt5.symbol_select.side_effect = mock_symbol_select
    
    mock_tick = MagicMock()
    mock_tick.ask = 4430.4
    mock_mt5.symbol_info_tick.return_value = mock_tick
    mock_mt5.symbol_info.return_value = MagicMock(filling_mode=1)
    
    mock_result = MagicMock()
    mock_result.retcode = 10009
    mock_result.order = 77123
    mock_mt5.order_send.return_value = mock_result
    
    signal = {
        "type": SignalType.ENTRY,
        "action": "BUY",
        "symbol": "XAUUSD",
        "order_type": "LIMIT",
        "price": 4430.4,
        "sl": 4424.9
    }
    
    result = executor.execute_signal(signal, channel="channel-b", config={"SYMBOL_SUFFIX": "m", "FX_PROVIDER_B_FIXED_LOT": "0.01"})
    assert "Trade opened: BUY XAUUSDm" in result
    
    call_args = mock_mt5.order_send.call_args[0][0]
    assert call_args["symbol"] == "XAUUSDm"
    
    # Reset side effect
    mock_mt5.symbol_select.side_effect = None
    mock_mt5.symbol_select.return_value = True

def test_per_terminal_symbol_suffix_resolution():
    executor = MT5Executor()
    
    def mock_symbol_select(sym, enable):
        # Broker supports XAUUSDm, XAUUSDc, and raw XAUUSD
        return sym in ["XAUUSDm", "XAUUSDc", "XAUUSD"]
    mock_mt5.symbol_select.side_effect = mock_symbol_select

    mock_tick = MagicMock()
    mock_tick.ask = 2650.0
    mock_mt5.symbol_info_tick.return_value = mock_tick
    mock_mt5.symbol_info.return_value = MagicMock(filling_mode=1)
    mock_mt5.order_send.return_value = MagicMock(retcode=10009, order=1001)

    signal = {
        "type": SignalType.ENTRY,
        "action": "BUY",
        "symbol": "XAUUSD",
        "price": 2650.0
    }

    config = {
        "PROVIDER_A_SYMBOL_SUFFIX": "m",
        "PROVIDER_B_SYMBOL_SUFFIX": "c",
        "PROVIDER_A_FIXED_LOT": "0.01",
        "PROVIDER_B_FIXED_LOT": "0.01",
        "PROVIDER_C_FIXED_LOT": "0.01"
    }

    # 1. provider_a should resolve to XAUUSDm
    res_provider_a = executor.execute_signal(signal, channel="channel-a", config=config)
    assert "Trade opened: BUY XAUUSDm" in res_provider_a

    # 2. provider_b should resolve to XAUUSDc
    res_provider_b = executor.execute_signal(signal, channel="channel-b", config=config)
    assert "Trade opened: BUY XAUUSDc" in res_provider_b

    # 3. provider_c should also route to Terminal 2 and resolve to XAUUSDc
    res_provider_c = executor.execute_signal(signal, channel="channel-c", config=config)
    assert "Trade opened: BUY XAUUSDc" in res_provider_c

    # 4. Explicit empty suffix on Terminal 1 keeps raw XAUUSD without suffix
    config_empty_provider_a = {
        "PROVIDER_A_SYMBOL_SUFFIX": "",
        "PROVIDER_B_SYMBOL_SUFFIX": "c",
        "PROVIDER_A_FIXED_LOT": "0.01"
    }
    res_raw = executor.execute_signal(signal, channel="channel-a", config=config_empty_provider_a)
    assert "Trade opened: BUY XAUUSD" in res_raw

    mock_mt5.symbol_select.side_effect = None
    mock_mt5.symbol_select.return_value = True


def test_executor_duplicate_trade_prevention():
    executor = MT5Executor()
    mock_tick = MagicMock()
    mock_tick.ask = 4470.0
    mock_mt5.symbol_info_tick.return_value = mock_tick
    mock_mt5.symbol_info.return_value = MagicMock(filling_mode=1)
    mock_result = MagicMock(retcode=10009, order=99991)
    mock_mt5.order_send.return_value = mock_result

    signal = {
        "type": SignalType.ENTRY,
        "action": "BUY",
        "symbol": "XAUUSD",
        "order_type": "MARKET"
    }

    # First call succeeds
    res1 = executor.execute_signal(signal, channel="channel-a", config={"PROVIDER_A_XAU_FIXED_LOT": "0.01"})
    assert "Trade opened" in res1

    # Second immediate call is blocked by 10s deduplication
    res2 = executor.execute_signal(signal, channel="channel-a", config={"PROVIDER_A_XAU_FIXED_LOT": "0.01"})
    assert "Duplicate trade rejected" in res2

def test_risk_percentage_channel_mapping():
    executor = MT5Executor()
    executor._calculate_lot_size = MagicMock(return_value=0.08)
    
    mock_tick = MagicMock(ask=4430.4)
    mock_mt5.symbol_info_tick.return_value = mock_tick
    mock_mt5.symbol_info.return_value = MagicMock(filling_mode=1)
    mock_mt5.order_send.return_value = MagicMock(retcode=10009, order=112233)
    
    signal = {
        "type": SignalType.ENTRY,
        "action": "BUY",
        "symbol": "XAUUSD",
        "order_type": "LIMIT",
        "price": 4430.4,
        "sl": 4424.9
    }
    
    # Test channel-b mapping to PROVIDER_B_LOT_MODE / PROVIDER_B_RISK_PCT
    provider_b_config = {
        "PROVIDER_B_LOT_MODE": "RISK",
        "PROVIDER_B_RISK_PCT": "3.0",
        "PROVIDER_B_FIXED_LOT": "0.01"
    }
    executor.execute_signal(signal, channel="channel-b", config=provider_b_config)
    assert executor._calculate_lot_size.called
    args = executor._calculate_lot_size.call_args[0]
    assert args[3] == 3.0
    
    # Test channel-a mapping to PROVIDER_A_LOT_MODE / PROVIDER_A_RISK_PCT
    executor._calculate_lot_size.reset_mock()
    provider_a_config = {
        "PROVIDER_A_LOT_MODE": "RISK",
        "PROVIDER_A_RISK_PCT": "2.5",
        "PROVIDER_A_FIXED_LOT": "0.01"
    }
    # Use different symbol to bypass 10s deduplication
    signal["symbol"] = "EURUSD"
    executor.execute_signal(signal, channel="channel-a", config=provider_a_config)
    assert executor._calculate_lot_size.called
    args = executor._calculate_lot_size.call_args[0]
    assert args[3] == 2.5

def test_executor_dual_entry_limit_orders():
    """Dual entry limit orders should place two pending orders with configured split ratio."""
    executor = MT5Executor()
    mock_mt5.symbol_select.return_value = True
    mock_mt5.symbol_info_tick.return_value = MagicMock(ask=4475.0, bid=4474.5)
    mock_mt5.symbol_info.return_value = MagicMock(volume_min=0.01, volume_step=0.01, volume_max=100.0, filling_mode=1)
    
    res1 = MagicMock(retcode=10009, order=55001)
    res2 = MagicMock(retcode=10009, order=55002)
    mock_mt5.order_send.reset_mock()
    mock_mt5.order_send.side_effect = [res1, res2]

    executor._calculate_lot_size = MagicMock(side_effect=[0.03, 0.01])

    signal = {
        "type": SignalType.ENTRY,
        "action": "BUY",
        "symbol": "XAUUSD_DUAL",
        "order_type": "LIMIT",
        "price": 4470.1,
        "price2": 4463.5,
        "sl": 4457.1
    }

    config = {
        "PROVIDER_B_LOT_MODE": "RISK",
        "PROVIDER_B_RISK_PCT": "3.0",
        "PROVIDER_B_DUAL_ENTRY_SPLIT": "75/25"
    }

    res = executor.execute_signal(signal, channel="channel-b", config=config)
    assert "Placed 2 LIMIT orders" in res
    assert "55001" in res
    assert "55002" in res
    assert mock_mt5.order_send.call_count == 2
    
    call1_request = mock_mt5.order_send.call_args_list[0][0][0]
    call2_request = mock_mt5.order_send.call_args_list[1][0][0]
    assert call1_request["price"] == 4470.1
    assert call1_request["volume"] == 0.03
    assert call2_request["price"] == 4463.5
    assert call2_request["volume"] == 0.01
    assert call1_request["sl"] == 4457.1
    assert call2_request["sl"] == 4457.1
    mock_mt5.order_send.side_effect = None

def test_executor_security_safeguard_mismatched_login():
    """Executor must halt and block order if active MT5 account login does not match expected login."""
    executor = MT5Executor()
    mock_mt5.initialize.return_value = True
    
    # Active account is provider_b's login (100002), but signal arrives for provider_a (expected 100001)
    mock_acc = MagicMock()
    mock_acc.login = 100002
    mock_mt5.account_info.return_value = mock_acc
    
    try:
        with patch("os.path.exists", return_value=True):
            signal = {
                "type": SignalType.ENTRY,
                "action": "BUY",
                "symbol": "XAUUSD_SEC",
                "price": 4500.0,
            }
            res = executor.execute_signal(
                signal, 
                channel="channel-a", 
                config={"PROVIDER_A_MT5_LOGIN": "100001", "PROVIDER_A_MT5_PATH": r"C:\fake\terminal64.exe"}
            )
            assert "Error: Could not bind MT5 terminal context" in res
    finally:
        mock_mt5.account_info.return_value = MagicMock()


def test_provider_c_routes_to_provider_b_terminal_and_uses_provider_c_lot():
    """provider_c (#channel-c) signals must route to provider_b's terminal and respect PROVIDER_C_FIXED_LOT."""
    executor = MT5Executor()
    mock_mt5.initialize.return_value = True

    mock_tick = MagicMock()
    mock_tick.ask = 2640.0
    mock_tick.bid = 2639.5
    mock_mt5.symbol_select.return_value = True
    mock_mt5.symbol_info_tick.return_value = mock_tick
    mock_mt5.symbol_info.return_value = MagicMock(filling_mode=1)

    mock_acc = MagicMock()
    mock_acc.login = 100002
    mock_mt5.account_info.return_value = mock_acc

    mock_res = MagicMock()
    mock_res.retcode = 10009
    mock_res.order = 88123
    mock_mt5.order_send.return_value = mock_res

    signal = {
        "type": SignalType.ENTRY,
        "action": "BUY",
        "symbol": "XAUUSD",
        "price": 2640.0,
        "sl": 2634.0,
        "tp": 2643.0
    }

    config = {
        "PROVIDER_B_MT5_PATH": r"C:\fake\terminal64_provider_b.exe",
        "PROVIDER_B_MT5_LOGIN": "100002",
        "PROVIDER_C_LOT_MODE": "FIXED",
        "PROVIDER_C_FIXED_LOT": "0.05",
        "PROVIDER_B_FIXED_LOT": "0.01"
    }

    with patch("os.path.exists", return_value=True):
        res = executor.execute_signal(signal, channel="channel-c", config=config)
        assert "Trade opened" in res
        assert "88123" in res

        # Verify volume was resolved from PROVIDER_C_FIXED_LOT (0.05), not PROVIDER_B_FIXED_LOT (0.01)
        call_req = mock_mt5.order_send.call_args[0][0]
        assert call_req["volume"] == 0.05
        assert call_req["sl"] == 2634.0
        assert call_req["tp"] == 2643.0


def test_shift_sl_update():
    """Test executing a Shift SL command modifies position SL."""
    executor = MT5Executor()
    pos = MagicMock(ticket=201, magic=123456, type=0, volume=0.02, symbol="XAUUSD", sl=4330.0, tp=4450.0, profit=15.0)
    mock_mt5.positions_get.return_value = [pos]
    mock_mt5.order_send.reset_mock()
    mock_mt5.order_send.return_value = MagicMock(retcode=10009)

    signal = {"type": SignalType.SL_UPDATE, "symbol": "XAUUSD", "sl": 4351.0}
    res = executor.execute_signal(signal)
    assert "SL Updated" in res
    assert "4351.0" in res
    assert "201" in res
    call_req = mock_mt5.order_send.call_args[0][0]
    assert call_req["sl"] == 4351.0
    assert call_req["position"] == 201


def test_shift_tp_update():
    """Test executing a Shift TP command modifies position TP."""
    executor = MT5Executor()
    pos = MagicMock(ticket=202, magic=123456, type=0, volume=0.02, symbol="XAUUSD", sl=4330.0, tp=4400.0, profit=15.0)
    mock_mt5.positions_get.return_value = [pos]
    mock_mt5.order_send.reset_mock()
    mock_mt5.order_send.return_value = MagicMock(retcode=10009)

    signal = {"type": SignalType.TP_UPDATE, "symbol": "XAUUSD", "tp": 4444.0}
    res = executor.execute_signal(signal)
    assert "TP moved to" in res
    assert "4444.0" in res
    assert "202" in res
    call_req = mock_mt5.order_send.call_args[0][0]
    assert call_req["tp"] == 4444.0
    assert call_req["position"] == 202


def test_breakeven_trigger_immediate_when_already_hit():
    """If CMP is already at/past trigger price, breakeven executes immediately."""
    executor = MT5Executor()
    pos = MagicMock(ticket=301, magic=123456, type=0, volume=0.05, symbol="XAUUSD", price_open=4350.0, sl=4330.0, tp=4450.0, profit=50.0)
    mock_mt5.positions_get.return_value = [pos]
    
    # CMP is 4395.0, trigger is 4390.0 for BUY -> already hit
    mock_tick = MagicMock(bid=4395.0, ask=4395.5)
    mock_mt5.symbol_info_tick.return_value = mock_tick
    mock_mt5.order_send.reset_mock()
    mock_mt5.order_send.return_value = MagicMock(retcode=10009)

    signal = {"type": SignalType.BREAKEVEN_TRIGGER, "symbol": "XAUUSD", "trigger_price": 4390.0, "price": 4390.0}
    res = executor.execute_signal(signal)
    assert "Breakeven Trigger Executed" in res
    assert "4350.0" in res
    call_req = mock_mt5.order_send.call_args[0][0]
    assert call_req["sl"] == 4350.0  # entry price
    assert call_req["position"] == 301


def test_breakeven_trigger_arms_when_not_yet_hit():
    """If CMP has not yet reached trigger price, trigger is armed in active_breakeven_triggers."""
    executor = MT5Executor()
    pos = MagicMock(ticket=302, magic=123456, type=0, volume=0.05, symbol="XAUUSD", price_open=4350.0, sl=4330.0, tp=4450.0, profit=10.0)
    mock_mt5.positions_get.return_value = [pos]
    
    # CMP is 4370.0, trigger is 4390.0 for BUY -> not yet reached
    mock_tick = MagicMock(bid=4370.0, ask=4370.5)
    mock_mt5.symbol_info_tick.return_value = mock_tick

    signal = {"type": SignalType.BREAKEVEN_TRIGGER, "symbol": "XAUUSD", "trigger_price": 4390.0, "price": 4390.0}
    res = executor.execute_signal(signal)
    assert "Breakeven Trigger Armed" in res
    assert "4390.0" in res
    assert any(t["symbol"] == "XAUUSD" and t["trigger_price"] == 4390.0 for t in executor.active_breakeven_triggers)


def test_executor_dedicated_terminal_targeting():
    """Verify that TARGET_TERMINAL forces binding to the selected terminal regardless of signal channel."""
    executor = MT5Executor()
    mock_mt5.initialize.reset_mock()
    mock_mt5.account_info.return_value = MagicMock(login=100001)

    cfg_term1 = {
        "TARGET_TERMINAL": "terminal_1",
        "PROVIDER_A_MT5_PATH": r"C:\Custom\provider_a\terminal64.exe",
        "PROVIDER_B_MT5_PATH": r"C:\Custom\provider_b\terminal64.exe"
    }

    # Even though channel is channel-b, TARGET_TERMINAL="terminal_1" binds to provider_a's terminal!
    with patch("os.path.exists", return_value=True):
        ok = executor._ensure_terminal_for_channel("channel-b", cfg_term1)
        assert ok is True
        mock_mt5.initialize.assert_called_with(path=r"C:\Custom\provider_a\terminal64.exe")

    # Conversely, even though channel is channel-a, TARGET_TERMINAL="terminal_2" binds to provider_b's terminal!
    cfg_term2 = {
        "TARGET_TERMINAL": "terminal_2",
        "PROVIDER_A_MT5_PATH": r"C:\Custom\provider_a\terminal64.exe",
        "PROVIDER_B_MT5_PATH": r"C:\Custom\provider_b\terminal64.exe"
    }
    with patch("os.path.exists", return_value=True):
        ok2 = executor._ensure_terminal_for_channel("channel-a", cfg_term2)
        assert ok2 is True
        mock_mt5.initialize.assert_called_with(path=r"C:\Custom\provider_b\terminal64.exe")

    # And TARGET_TERMINAL="terminal_3" binds to terminal_owner's terminal!
    cfg_term3 = {
        "TARGET_TERMINAL": "terminal_3",
        "TERMINAL_OWNER_MT5_PATH": r"C:\Custom\terminal_owner\terminal64.exe"
    }
    with patch("os.path.exists", return_value=True):
        ok3 = executor._ensure_terminal_for_channel("channel-a", cfg_term3)
        assert ok3 is True
        mock_mt5.initialize.assert_called_with(path=r"C:\Custom\terminal_owner\terminal64.exe")


def test_executor_per_terminal_lot_sizing():
    """Verify that when a signal is executed, lot sizing pulls from the active MT5 terminal settings."""
    executor = MT5Executor()
    mock_mt5.symbol_select.return_value = True
    mock_mt5.symbol_info_tick.return_value = MagicMock(ask=4350.0, bid=4349.5)
    mock_mt5.symbol_info.return_value = MagicMock(volume_min=0.01, volume_max=100.0, volume_step=0.01, filling_mode=2)
    mock_mt5.order_send.return_value = MagicMock(retcode=10009, order=55555)

    # 1. House challenge executed on Terminal 1 -> uses Terminal 1 fixed lot (0.12)
    cfg1 = {
        "TARGET_TERMINAL": "terminal_1",
        "TERMINAL_1_LOT_MODE": "FIXED",
        "TERMINAL_1_FIXED_LOT": "0.12",
        "TERMINAL_3_LOT_MODE": "FIXED",
        "TERMINAL_3_FIXED_LOT": "0.02"
    }
    with patch("os.path.exists", return_value=True):
        res1 = executor.execute_signal({"type": SignalType.ENTRY, "action": "BUY", "symbol": "XAUUSD"}, channel="channel-a", config=cfg1)
        assert "55555" in res1
        req1 = mock_mt5.order_send.call_args[0][0]
        assert req1["volume"] == 0.12

    # Clear deduplication cache
    executor._recent_trades.clear()

    # 2. Same House challenge executed on Terminal 3 -> uses Terminal 3 fixed lot (0.02), NOT 0.12!
    cfg3 = {
        "TARGET_TERMINAL": "terminal_3",
        "TERMINAL_1_LOT_MODE": "FIXED",
        "TERMINAL_1_FIXED_LOT": "0.12",
        "TERMINAL_3_LOT_MODE": "FIXED",
        "TERMINAL_3_FIXED_LOT": "0.02"
    }
    with patch("os.path.exists", return_value=True):
        res3 = executor.execute_signal({"type": SignalType.ENTRY, "action": "BUY", "symbol": "XAUUSD"}, channel="channel-a", config=cfg3)
        assert "55555" in res3
        req3 = mock_mt5.order_send.call_args[0][0]
        assert req3["volume"] == 0.02


def test_half_risk_fixed_lot_mode():
    executor = MT5Executor()
    mock_mt5.symbol_select.return_value = True
    mock_mt5.symbol_info_tick.return_value = MagicMock(ask=2000.0, bid=1999.0)
    mock_mt5.symbol_info.return_value = MagicMock(volume_min=0.01, volume_max=100.0, volume_step=0.01, filling_mode=2)
    mock_mt5.order_send.return_value = MagicMock(retcode=10009, order=77777)

    cfg = {
        "TARGET_TERMINAL": "terminal_1",
        "TERMINAL_1_LOT_MODE": "FIXED",
        "TERMINAL_1_FIXED_LOT": "0.10"
    }
    
    # 1. Normal trade with no risk multiplier
    executor._recent_trades.clear()
    with patch("os.path.exists", return_value=True):
        res_normal = executor.execute_signal(
            {"type": SignalType.ENTRY, "action": "BUY", "symbol": "XAUUSD"},
            channel="provider_a",
            config=cfg
        )
        assert "77777" in res_normal
        req_normal = mock_mt5.order_send.call_args[0][0]
        assert req_normal["volume"] == 0.10

    # 2. Half risk trade (risk_multiplier = 0.5) by default scales lot to 0.05 (e.g. 5.0 -> 2.5 by design)
    executor._recent_trades.clear()
    with patch("os.path.exists", return_value=True):
        res_half = executor.execute_signal(
            {"type": SignalType.ENTRY, "action": "BUY", "symbol": "XAUUSD", "risk_multiplier": 0.5, "half_risk": True},
            channel="provider_a",
            config=cfg
        )
        assert "77777" in res_half
        assert "0.5R Risk" in res_half
        req_half = mock_mt5.order_send.call_args[0][0]
        assert req_half["volume"] == 0.05
        assert "0.5R" in req_half["comment"]

    # 3. Half risk trade with APPLY_SIGNAL_RISK_TO_FIXED_LOT=false preserves raw fixed lot at 0.10
    executor._recent_trades.clear()
    cfg_raw = dict(cfg, APPLY_SIGNAL_RISK_TO_FIXED_LOT="false")
    with patch("os.path.exists", return_value=True):
        res_raw = executor.execute_signal(
            {"type": SignalType.ENTRY, "action": "BUY", "symbol": "XAUUSD", "risk_multiplier": 0.5, "half_risk": True},
            channel="provider_a",
            config=cfg_raw
        )
        assert "77777" in res_raw
        req_raw = mock_mt5.order_send.call_args[0][0]
        assert req_raw["volume"] == 0.10


def test_half_risk_percentage_mode():
    executor = MT5Executor()
    mock_mt5.symbol_select.return_value = True
    mock_mt5.symbol_info_tick.return_value = MagicMock(ask=2000.0, bid=1999.0)
    mock_mt5.symbol_info.return_value = MagicMock(
        volume_min=0.01, volume_max=100.0, volume_step=0.01, filling_mode=2,
        trade_tick_size=0.01, trade_tick_value=1.0
    )
    mock_mt5.account_info.return_value = MagicMock(balance=10000.0)
    mock_mt5.order_send.return_value = MagicMock(retcode=10009, order=88888)

    cfg = {
        "TARGET_TERMINAL": "terminal_1",
        "TERMINAL_1_LOT_MODE": "RISK",
        "TERMINAL_1_RISK_PCT": "2.0",
        "TERMINAL_1_FIXED_LOT": "0.01"
    }

    # Balance = 10,000. Normal risk 2% = 200 risk.
    # Entry = 2000, SL = 1990 -> 10 points loss = 1000 ticks * 1.0 = 1000 per lot.
    # Normal lot = 200 / 1000 = 0.20 lots.
    # Half risk (1% risk = 100 risk) -> lot = 100 / 1000 = 0.10 lots!
    executor._recent_trades.clear()
    with patch("os.path.exists", return_value=True):
        res = executor.execute_signal(
            {
                "type": SignalType.ENTRY,
                "action": "BUY",
                "symbol": "XAUUSD",
                "price": 2000.0,
                "sl": 1990.0,
                "risk_multiplier": 0.5,
                "half_risk": True
            },
            channel="provider_a",
            config=cfg
        )
        assert "88888" in res
        assert "0.5R Risk" in res
        req = mock_mt5.order_send.call_args[0][0]
        assert req["volume"] == 0.10

def test_provider_d_breakeven_moves_sl_to_price_open():
    executor = MT5Executor()
    mock_pos = MagicMock()
    mock_pos.ticket = 991122
    mock_pos.symbol = "XAUUSD"
    mock_pos.price_open = 4150.25
    mock_pos.tp = 4200.0
    mock_pos.volume = 0.05
    mock_pos.profit = 15.0
    mock_pos.magic = executor.magic_number

    mock_mt5.positions_get.return_value = [mock_pos]
    mock_mt5.order_send.return_value = MagicMock(retcode=10009)

    signal = {
        "type": SignalType.SL_UPDATE,
        "symbol": "XAUUSD",
        "sl": 0.0,
        "breakeven": True
    }
    with patch("os.path.exists", return_value=True):
        res = executor.execute_signal(signal, channel="provider_d")
        assert "Breakeven" in res
        req = mock_mt5.order_send.call_args[0][0]
        assert req["sl"] == 4150.25
        assert req["position"] == 991122

def test_provider_d_terminal_routing_and_lot_sizing():
    executor = MT5Executor()
    mock_tick = MagicMock(ask=4150.0, bid=4149.0)
    mock_mt5.symbol_select.return_value = True
    mock_mt5.symbol_info_tick.return_value = mock_tick
    mock_mt5.symbol_info.return_value = MagicMock(volume_min=0.01, volume_max=100.0, volume_step=0.01, filling_mode=1)
    mock_mt5.order_send.return_value = MagicMock(retcode=10009, order=77112)

    cfg = {
        "PROVIDER_D_TARGET_TERMINAL": "terminal_2",
        "PROVIDER_D_LOT_MODE": "FIXED",
        "PROVIDER_D_FIXED_LOT": "0.08"
    }

    signal = {
        "type": SignalType.ENTRY,
        "action": "BUY",
        "symbol": "XAUUSD",
        "price": 4150.0,
        "sl": 4140.0
    }
    executor._recent_trades.clear()
    with patch("os.path.exists", return_value=True):
        res = executor.execute_signal(signal, channel="provider_d", config=cfg)
        assert "77112" in res
        req = mock_mt5.order_send.call_args[0][0]
        assert req["volume"] == 0.08


def test_calculate_MILESTONE_risk_milestones_and_tiers():
    """Verify exact formula matches Excel model across milestone doublings and boost tiers."""
    # 1. At starting milestone ($100)
    r100 = MT5Executor.calculate_MILESTONE_risk(100.0, starting_milestone=100.0)
    assert r100["milestone"] == 100.0
    assert r100["base_risk"] == 5.0
    assert r100["profit_above_milestone"] == 0.0
    assert r100["boost"] == 0.0
    assert r100["total_risk"] == 5.0
    assert r100["tier"] == 1

    # 2. Tier 1 boost (< 1.5 * M): $130 balance -> profit $30, boost = 15% * $30 = $4.50
    r130 = MT5Executor.calculate_MILESTONE_risk(130.0, starting_milestone=100.0)
    assert r130["milestone"] == 100.0
    assert r130["base_risk"] == 5.0
    assert r130["profit_above_milestone"] == 30.0
    assert r130["boost"] == 4.5
    assert r130["total_risk"] == 9.5
    assert r130["tier"] == 1
    assert r130["effective_risk_pct"] == 7.31

    # 3. Tier 2 boost (1.5 * M <= bal < 1.8 * M): $160 balance -> profit $60, boost = 12% * $60 = $7.20
    r160 = MT5Executor.calculate_MILESTONE_risk(160.0, starting_milestone=100.0)
    assert r160["milestone"] == 100.0
    assert r160["base_risk"] == 5.0
    assert r160["profit_above_milestone"] == 60.0
    assert r160["boost"] == 7.2
    assert r160["total_risk"] == 12.2
    assert r160["tier"] == 2
    assert r160["effective_risk_pct"] in (7.62, 7.63)

    # 4. Tier 3 boost (bal >= 1.8 * M): $190 balance -> profit $90, boost = 9% * $90 = $8.10
    r190 = MT5Executor.calculate_MILESTONE_risk(190.0, starting_milestone=100.0)
    assert r190["milestone"] == 100.0
    assert r190["base_risk"] == 5.0
    assert r190["profit_above_milestone"] == 90.0
    assert r190["boost"] == 8.1
    assert r190["total_risk"] == 13.1
    assert r190["tier"] == 3
    assert r190["effective_risk_pct"] == 6.89

    # 5. Milestone Doubling at $200: Milestone resets to $200, Base = $10 (5% of 200), Boost = 0
    r200 = MT5Executor.calculate_MILESTONE_risk(200.0, starting_milestone=100.0)
    assert r200["milestone"] == 200.0
    assert r200["base_risk"] == 10.0
    assert r200["profit_above_milestone"] == 0.0
    assert r200["boost"] == 0.0
    assert r200["total_risk"] == 10.0

    # 6. Compounded balance $300: Milestone is $200, profit $100 -> 300 >= 1.5 * 200 (Tier 2, 12% boost)
    # Boost = 12% of $100 = $12.00, Base = $10.00, Total = $22.00
    r300 = MT5Executor.calculate_MILESTONE_risk(300.0, starting_milestone=100.0)
    assert r300["milestone"] == 200.0
    assert r300["base_risk"] == 10.0
    assert r300["profit_above_milestone"] == 100.0
    assert r300["boost"] == 12.0
    assert r300["total_risk"] == 22.0
    assert r300["tier"] == 2

    # 7. Milestone Doubling at $400: Milestone resets to $400, Base = $20, Boost = 0
    r400 = MT5Executor.calculate_MILESTONE_risk(400.0, starting_milestone=100.0)
    assert r400["milestone"] == 400.0
    assert r400["base_risk"] == 20.0
    assert r400["boost"] == 0.0
    assert r400["total_risk"] == 20.0

    # 8. Below initial milestone: balance $50 -> milestone remains $100, Base = $5, Boost = 0
    r50 = MT5Executor.calculate_MILESTONE_risk(50.0, starting_milestone=100.0)
    assert r50["milestone"] == 100.0
    assert r50["base_risk"] == 5.0
    assert r50["profit_above_milestone"] == 0.0
    assert r50["boost"] == 0.0
    assert r50["total_risk"] == 5.0


def test_calculate_lot_from_money():
    """Verify lot size calculation from target dollar risk."""
    executor = MT5Executor()
    mock_info = MagicMock(
        trade_tick_size=0.01,
        trade_tick_value=1.0,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01
    )
    mock_mt5.symbol_info.return_value = mock_info

    # 100 pips ($10 SL distance on Gold). Ticks lost = 10 / 0.01 = 1000. Loss per lot = 1000 * 1.0 = $1000
    # Desired cash risk: $10.00 -> lot size = 10 / 1000 = 0.01
    lot = executor._calculate_lot_from_money("XAUUSD", entry_price=2000.0, sl_price=1990.0, risk_money=10.0)
    assert lot == 0.01

    # Desired cash risk: $50.00 -> lot size = 50 / 1000 = 0.05
    lot50 = executor._calculate_lot_from_money("XAUUSD", entry_price=2000.0, sl_price=1990.0, risk_money=50.0)
    assert lot50 == 0.05


def test_provider_d_execution_MILESTONE():
    """Test full execution flow with PROVIDER_D_LOT_MODE='MILESTONE' and starting milestone."""
    executor = MT5Executor()
    mock_tick = MagicMock(ask=4150.0, bid=4149.0)
    mock_mt5.symbol_select.return_value = True
    mock_mt5.symbol_info_tick.return_value = mock_tick
    mock_mt5.symbol_info.return_value = MagicMock(
        trade_tick_size=0.01,
        trade_tick_value=1.0,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        filling_mode=1
    )
    mock_mt5.account_info.return_value = MagicMock(balance=130.0)
    mock_mt5.order_send.return_value = MagicMock(retcode=10009, order=88231)

    cfg = {
        "PROVIDER_D_TARGET_TERMINAL": "terminal_1",
        "PROVIDER_D_LOT_MODE": "MILESTONE",
        "PROVIDER_D_STARTING_MILESTONE": "100.0"
    }

    # Signal with SL 10 points away: 4150 - 4140 = 10.0. Loss per lot = (10 / 0.01) * 1.0 = $1000/lot.
    # At balance $130, Milestone risk = Base $5 + Boost $4.50 (15% of $30) = $9.50.
    # Lot = 9.50 / 1000 = 0.0095 -> rounded to volume_step (0.01) = 0.01
    signal = {
        "type": SignalType.ENTRY,
        "action": "BUY",
        "symbol": "XAUUSD",
        "price": 4150.0,
        "sl": 4140.0
    }
    executor._recent_trades.clear()
    with patch("os.path.exists", return_value=True):
        res = executor.execute_signal(signal, channel="provider_d", config=cfg)
        assert "88231" in res
        req = mock_mt5.order_send.call_args[0][0]
        assert req["volume"] == 0.01


def test_provider_d_execution_MILESTONE_with_half_risk():
    """Test Milestone lot sizing with a 0.5R half risk multiplier."""
    executor = MT5Executor()
    mock_tick = MagicMock(ask=4150.0, bid=4149.0)
    mock_mt5.symbol_select.return_value = True
    mock_mt5.symbol_info_tick.return_value = mock_tick
    mock_mt5.symbol_info.return_value = MagicMock(
        trade_tick_size=0.01,
        trade_tick_value=1.0,
        volume_min=0.01,
        volume_max=100.0,
        volume_step=0.01,
        filling_mode=1
    )
    # Balance $400 -> Milestone $400, Base $20, Boost $0 -> Total Risk = $20.00
    mock_mt5.account_info.return_value = MagicMock(balance=400.0)
    mock_mt5.order_send.return_value = MagicMock(retcode=10009, order=88232)

    cfg = {
        "PROVIDER_D_TARGET_TERMINAL": "terminal_1",
        "PROVIDER_D_LOT_MODE": "MILESTONE",
        "PROVIDER_D_STARTING_MILESTONE": "100.0"
    }

    # SL 2 points away: 4150 - 4148 = 2.0. Loss per lot = 200 * 1 = $200/lot.
    # With 0.5R multiplier, Total Risk becomes $20 * 0.5 = $10.00.
    # Lot = 10.00 / 200 = 0.05 lots!
    signal = {
        "type": SignalType.ENTRY,
        "action": "BUY",
        "symbol": "XAUUSD",
        "price": 4150.0,
        "sl": 4148.0,
        "risk_multiplier": 0.5
    }
    executor._recent_trades.clear()
    with patch("os.path.exists", return_value=True):
        res = executor.execute_signal(signal, channel="provider_d", config=cfg)
        assert "88232" in res
        req = mock_mt5.order_send.call_args[0][0]
        assert req["volume"] == 0.05


def test_adopt_ticket_allows_modification_of_manual_trade():
    """Manual trades with magic=0 cannot be modified until adopted by the executor."""
    executor = MT5Executor()
    manual_pos = MagicMock()
    manual_pos.ticket = 999888
    manual_pos.magic = 0  # Manual / phone trade
    manual_pos.symbol = "XAUUSD"
    manual_pos.price_open = 2650.0
    manual_pos.sl = 0.0
    manual_pos.tp = 0.0
    manual_pos.volume = 0.10
    manual_pos.profit = 15.0
    manual_pos.type = 0  # BUY

    mock_mt5.positions_get.return_value = [manual_pos]
    mock_mt5.symbol_select.return_value = True

    # 1. Before adoption: unmanaged
    positions_before = executor._find_all_positions("XAUUSD")
    assert len(positions_before) == 0

    # 2. Adopt ticket
    executor.adopt_ticket(999888, symbol="XAUUSD", channel="provider_d")
    assert executor.is_ticket_managed(999888, magic=0) is True

    # 3. After adoption: returned in _find_all_positions
    positions_after = executor._find_all_positions("XAUUSD")
    assert len(positions_after) == 1
    assert positions_after[0].ticket == 999888

    # 4. SL Update on adopted trade
    mock_result = MagicMock(retcode=10009, comment="Done")
    mock_mt5.order_send.return_value = mock_result
    sl_res = executor._update_sl({"type": SignalType.SL_UPDATE, "symbol": "XAUUSD", "sl": 2640.0})
    assert "SL Updated" in sl_res
    assert "999888" in sl_res
    req = mock_mt5.order_send.call_args[0][0]
    assert req["position"] == 999888
    assert req["sl"] == 2640.0

    # 5. Close adopted trade
    mock_tick = MagicMock(bid=2655.0, ask=2655.5)
    mock_mt5.symbol_info_tick.return_value = mock_tick
    close_res = executor._close_position({"type": SignalType.CLOSE_POSITION, "symbol": "XAUUSD"})
    assert "Position closed" in close_res
    assert "999888" in close_res
    # Should be unadopted upon successful close
    assert 999888 not in executor.adopted_tickets

def test_open_trade_auto_closes_stale_position():
    """When a previous trade exists from minutes ago, a new MARKET entry should auto-close it first."""
    import time
    executor = MT5Executor(magic_number=123456, pip_thresholds={"XAUUSD": 100})
    
    stale_pos = MagicMock(ticket=111222, magic=123456, symbol="XAUUSD", volume=0.03, type=0, profit=-25.0, time=time.time() - 300)
    mock_mt5.positions_get.return_value = [stale_pos]
    mock_mt5.symbol_select.return_value = True
    mock_tick = MagicMock(ask=2650.0, bid=2649.5)
    mock_mt5.symbol_info_tick.return_value = mock_tick
    mock_mt5.symbol_info.return_value = MagicMock(filling_mode=1)
    
    mock_result = MagicMock(retcode=10009, order=555666)
    mock_mt5.order_send.return_value = mock_result
    
    signal = {"type": SignalType.ENTRY, "action": "BUY", "symbol": "XAUUSD", "price": 2650.0}
    res = executor.execute_signal(signal, channel="channel-a", config={"AUTO_CLOSE_PREVIOUS_ON_NEW_ENTRY": "true"})
    
    assert "Auto-closed previous trade(s)" in res
    assert "111222" in res
    assert "Trade opened" in res
    assert "555666" in res
    # order_send should have been called twice: 1 close, 1 open
    assert mock_mt5.order_send.call_count >= 2

def test_open_trade_does_not_close_recent_dual_entry():
    """Positions opened within 15 seconds should NOT be auto-closed (protects dual entries)."""
    import time
    executor = MT5Executor(magic_number=123456, pip_thresholds={"XAUUSD": 100})
    
    recent_pos = MagicMock(ticket=333444, magic=123456, symbol="XAUUSD", volume=0.03, type=0, profit=0.0, time=time.time() - 5)
    mock_mt5.positions_get.return_value = [recent_pos]
    mock_mt5.symbol_select.return_value = True
    mock_tick = MagicMock(ask=2650.0, bid=2649.5)
    mock_mt5.symbol_info_tick.return_value = mock_tick
    mock_mt5.symbol_info.return_value = MagicMock(filling_mode=1)
    
    mock_result = MagicMock(retcode=10009, order=777888)
    mock_mt5.order_send.return_value = mock_result
    
    signal = {"type": SignalType.ENTRY, "action": "BUY", "symbol": "XAUUSD", "price": 2650.0}
    res = executor.execute_signal(signal, channel="channel-a", config={"AUTO_CLOSE_PREVIOUS_ON_NEW_ENTRY": "true"})
    
    assert "Auto-closed" not in res
    assert "Trade opened" in res
    assert "777888" in res

def test_open_trade_auto_closes_multiple_stale_positions():
    """All stale positions for the channel should be auto-closed, not just the first one."""
    import time
    executor = MT5Executor(magic_number=123456, pip_thresholds={"XAUUSD": 100})
    
    pos1 = MagicMock(ticket=101, magic=123456, symbol="XAUUSD", volume=0.02, type=0, profit=10.0, time=time.time() - 200)
    pos2 = MagicMock(ticket=102, magic=123456, symbol="XAUUSD", volume=0.03, type=0, profit=-5.0, time=time.time() - 150)
    mock_mt5.positions_get.return_value = [pos1, pos2]
    mock_mt5.symbol_select.return_value = True
    mock_tick = MagicMock(ask=2650.0, bid=2649.5)
    mock_mt5.symbol_info_tick.return_value = mock_tick
    mock_mt5.symbol_info.return_value = MagicMock(filling_mode=1)
    
    mock_result = MagicMock(retcode=10009, order=999)
    mock_mt5.order_send.return_value = mock_result
    
    signal = {"type": SignalType.ENTRY, "action": "BUY", "symbol": "XAUUSD", "price": 2650.0}
    res = executor.execute_signal(signal, channel="channel-a", config={"AUTO_CLOSE_PREVIOUS_ON_NEW_ENTRY": "true"})
    
    assert "101" in res
    assert "102" in res
    assert "999" in res
    # 2 close orders + 1 open order = 3 order_send calls
    assert mock_mt5.order_send.call_count >= 3

def test_open_trade_does_not_close_different_channel_position():
    """Stale positions assigned to a different channel must NOT be auto-closed."""
    import time
    executor = MT5Executor(magic_number=123456, pip_thresholds={"XAUUSD": 100})
    executor.adopt_ticket(888, symbol="XAUUSD", channel="provider_b")
    
    other_channel_pos = MagicMock(ticket=888, magic=123456, symbol="XAUUSD", volume=0.05, type=0, profit=50.0, time=time.time() - 300)
    mock_mt5.positions_get.return_value = [other_channel_pos]
    mock_mt5.symbol_select.return_value = True
    mock_tick = MagicMock(ask=2650.0, bid=2649.5)
    mock_mt5.symbol_info_tick.return_value = mock_tick
    mock_mt5.symbol_info.return_value = MagicMock(filling_mode=1)
    
    mock_result = MagicMock(retcode=10009, order=777)
    mock_mt5.order_send.return_value = mock_result
    
    signal = {"type": SignalType.ENTRY, "action": "BUY", "symbol": "XAUUSD", "price": 2650.0}
    res = executor.execute_signal(signal, channel="channel-a", config={"AUTO_CLOSE_PREVIOUS_ON_NEW_ENTRY": "true"})
    
    assert "Auto-closed" not in res
    assert "888" not in res
    assert 888 in executor.adopted_tickets
    assert "777" in res











