import pytest
from src.parser import parse_signal, SignalType

def test_parse_buy_signal():
    msg = "BUY XAUUSD CMP 4625.9"
    signal = parse_signal(msg)
    assert signal["type"] == SignalType.ENTRY
    assert signal["action"] == "BUY"
    assert signal["symbol"] == "XAUUSD"
    assert signal["price"] == 4625.9

def test_parse_sell_signal():
    msg = "SELL EURUSD CMP 1.0850"
    signal = parse_signal(msg)
    assert signal["type"] == SignalType.ENTRY
    assert signal["action"] == "SELL"
    assert signal["symbol"] == "EURUSD"
    assert signal["price"] == 1.0850

def test_parse_sl_update():
    msg = "XAUUSD Set SL to 4611.0 (-149 pips)"
    signal = parse_signal(msg)
    assert signal["type"] == SignalType.SL_UPDATE
    assert signal["symbol"] == "XAUUSD"
    assert signal["sl"] == 4611.0

def test_parse_tp_open():
    msg = "XAUUSD\nTP OPEN"
    signal = parse_signal(msg)
    assert signal["type"] == SignalType.TP_UPDATE
    assert signal["symbol"] == "XAUUSD"

def test_parse_provider_b_entry_with_mentions_and_sl():
    msg = "@provider_b @Forex Alert\nBUY XAUUSD CMP 4526.5\n\nSL : 4520.0 (-65 pips)"
    signal = parse_signal(msg)
    assert signal["type"] == SignalType.ENTRY
    assert signal["action"] == "BUY"
    assert signal["symbol"] == "XAUUSD"
    assert signal["price"] == 4526.5
    assert signal["sl"] == 4520.0

def test_parse_entry_with_sl_and_tp():
    msg = "BUY XAUUSD CMP 4526.5\nSL: 4520.0\nTP 1: 4540.0"
    signal = parse_signal(msg)
    assert signal["type"] == SignalType.ENTRY
    assert signal["action"] == "BUY"
    assert signal["symbol"] == "XAUUSD"
    assert signal["price"] == 4526.5
    assert signal["sl"] == 4520.0
    assert signal["tp"] == 4540.0

def test_parse_move_sl_breakeven():
    msg = "@provider_b @Forex Alert\nXAUUSD Move SL to Breakeven (4526.5)"
    signal = parse_signal(msg)
    assert signal["type"] == SignalType.SL_UPDATE
    assert signal["symbol"] == "XAUUSD"
    assert signal["sl"] == 4526.5

def test_parse_sl_moved_to_breakeven():
    msg = "@everyone @Forex Alert\nXAUUSD SL moved to Breakeven (4581.2)"
    signal = parse_signal(msg)
    assert signal["type"] == SignalType.SL_UPDATE
    assert signal["symbol"] == "XAUUSD"
    assert signal["sl"] == 4581.2

def test_parse_take_tp_alert():
    msg = "@provider_b @Forex Alert\nXAUUSD Take TP 1 here at 4533.2 (+67 pips (1R))"
    signal = parse_signal(msg)
    assert signal["type"] == SignalType.CLOSE_POSITION
    assert signal["symbol"] == "XAUUSD"

def test_parse_trade_closed():
    msg = "Wednesday, 26 August 2026 at 14:18 @provider_b @Forex Alert XAUUSD Trade Closed (4618.9) Result: -2 pips (0R)"
    signal = parse_signal(msg)
    assert signal["type"] == SignalType.CLOSE_POSITION
    assert signal["symbol"] == "XAUUSD"

def test_parse_unknown_signal():
    msg = "Hello everyone, market is volatile today"
    signal = parse_signal(msg)
    assert signal["type"] == SignalType.UNKNOWN

def test_parse_entry_without_cmp_price():
    msg = "BUY XAUUSD"
    signal = parse_signal(msg)
    assert signal["type"] == SignalType.UNKNOWN

def test_parse_provider_b_standalone_limit_entry():
    msg = "@provider_b @Forex Alert\n\nEntry : 4430.4\nSL : 4424.9 (-56 pips)\n\nManage Risk."
    signal = parse_signal(msg)
    assert signal["type"] == SignalType.ENTRY
    assert signal["action"] == "BUY"
    assert signal["symbol"] == "XAUUSD"
    assert signal["order_type"] == "LIMIT"
    assert signal["price"] == 4430.4
    assert signal["sl"] == 4424.9

def test_parse_breakeven_set_at():
    msg = "@everyone @Forex Alert\nXAUUSD Breakeven set at 4440.0"
    signal = parse_signal(msg)
    assert signal["type"] == SignalType.BREAKEVEN_TRIGGER
    assert signal["symbol"] == "XAUUSD"
    assert signal["trigger_price"] == 4440.0

def test_parse_shift_sl():
    msg = "XAUUSD Shift SL to 4351.0 (-224 pips)"
    signal = parse_signal(msg)
    assert signal["type"] == SignalType.SL_UPDATE
    assert signal["symbol"] == "XAUUSD"
    assert signal["sl"] == 4351.0

def test_parse_shift_tp():
    msg = "XAUUSD Shift TP to 4444.0"
    signal = parse_signal(msg)
    assert signal["type"] == SignalType.TP_UPDATE
    assert signal["symbol"] == "XAUUSD"
    assert signal["tp"] == 4444.0

def test_parse_breakeven_trigger_variants():
    signals = [
        ("XAUUSD Breakeven set at 4390.0", "XAUUSD", 4390.0),
        ("Breakeven set at 4390.0", "XAUUSD", 4390.0),
        ("XAUUSD Set Breakeven at 4395.5", "XAUUSD", 4395.5),
        ("EURUSD Breakeven target at 1.0950", "EURUSD", 1.0950),
    ]
    for text, expected_sym, expected_price in signals:
        sig = parse_signal(text)
        assert sig["type"] == SignalType.BREAKEVEN_TRIGGER
        assert sig["symbol"] == expected_sym
        assert sig["trigger_price"] == expected_price

def test_parse_conversational_close_warnings_rejected():
    conversational_messages = [
        "i might close this trade if 4h candle below idk elts say 4400",
        "XAUUSD if 4h candle close below 4400",
        "Close XAUUSD if 4h candle closes below 4400",
        "Watch the 4h candle close for XAUUSD",
        "Might close XAUUSD if it rejects 4450",
        "Wait for 4h candle close on XAUUSD",
        "Could close early if we see rejection",
        "Watching candle close below 4420",
        "market close is approaching",
        "daily close was bullish",
        "if candle closes below 4400 i will exit"
    ]
    for msg in conversational_messages:
        sig = parse_signal(msg)
        assert sig["type"] == SignalType.UNKNOWN, f"Message '{msg}' was incorrectly parsed as {sig}"

def test_parse_valid_close_commands():
    valid_close_signals = [
        ("XAUUSD Trade Closed (4618.9) Result: -2 pips (0R)", "XAUUSD"),
        ("XAUUSD Take TP 1 here at 4533.2 (+67 pips (1R))", "XAUUSD"),
        ("XAUUSD Take TP here", "XAUUSD"),
        ("Close XAUUSD", "XAUUSD"),
        ("Close XAUUSD now", "XAUUSD"),
        ("Close half XAUUSD", "XAUUSD"),
        ("Close all XAUUSD", "XAUUSD"),
        ("XAUUSD Closed", "XAUUSD"),
        ("@provider_b @Forex Alert\nClose XAUUSD now", "XAUUSD"),
        ("@everyone\nXAUUSD Trade Closed", "XAUUSD")
    ]
    for msg, expected_symbol in valid_close_signals:
        sig = parse_signal(msg)
        assert sig["type"] == SignalType.CLOSE_POSITION, f"Message '{msg}' failed to parse as CLOSE_POSITION"
        assert sig["symbol"] == expected_symbol, f"Message '{msg}' expected symbol {expected_symbol}, got {sig.get('symbol')}"

def test_parse_provider_b_dual_entry_limit():
    msg = """PLACE XAUUSD BUY LIMIT

Entry : 4470.1 | 4463.5
SL : 4457.1 (-130 pips)

Manage Risk"""
    sig = parse_signal(msg)
    assert sig["type"] == SignalType.ENTRY
    assert sig["symbol"] == "XAUUSD"
    assert sig["action"] == "BUY"
    assert sig["order_type"] == "LIMIT"
    assert sig["price"] == 4470.1
    assert sig["price2"] == 4463.5
    assert sig["sl"] == 4457.1

def test_parse_standalone_dual_entry_limit():
    msg = """Entry : 4470.1 - 4463.5
SL : 4457.1 (-130 pips)
XAUUSD BUY"""
    sig = parse_signal(msg)
    assert sig["type"] == SignalType.ENTRY
    assert sig["symbol"] == "XAUUSD"
    assert sig["action"] == "BUY"
    assert sig["order_type"] == "LIMIT"
    assert sig["price"] == 4470.1
    assert sig["price2"] == 4463.5
    assert sig["sl"] == 4457.1

def test_parse_discord_timestamp_and_headers_tp_update():
    msg = "[ 12:21 ] Monday, 21 September 2026 at 12:21 @example_role-provider_c @Forex Alert XAUUSD Set TP to 4363.0 (+89 pips)"
    sig = parse_signal(msg)
    assert sig["type"] == SignalType.TP_UPDATE
    assert sig["symbol"] == "XAUUSD"
    assert sig["tp"] == 4363.0

def test_parse_discord_timestamp_and_headers_sl_update():
    msg = "[ 12:21 ] Monday, 21 September 2026 at 12:21 @example_role-provider_c @Forex Alert XAUUSD Shift SL to 4351.0 (-224 pips)"
    sig = parse_signal(msg)
    assert sig["type"] == SignalType.SL_UPDATE
    assert sig["symbol"] == "XAUUSD"
    assert sig["sl"] == 4351.0

def test_parse_half_risk_parenthesized_r():
    msg = """Mohid APP 18:24
@Mohid @Forex Alert
BUY USOIL CMP 92.66

Half Risk (0.5r)

SL : 91.10"""
    sig = parse_signal(msg)
    assert sig["type"] == SignalType.ENTRY
    assert sig["action"] == "BUY"
    assert sig["symbol"] == "USOIL"
    assert sig["price"] == 92.66
    assert sig["sl"] == 91.10
    assert sig["risk_multiplier"] == 0.5
    assert sig["half_risk"] is True

def test_parse_half_risk_variants():
    # 1. "Half Risk"
    sig1 = parse_signal("BUY XAUUSD CMP 2650.0\nHalf Risk\nSL : 2640.0")
    assert sig1["risk_multiplier"] == 0.5
    assert sig1["half_risk"] is True

    # 2. "0.5r"
    sig2 = parse_signal("BUY XAUUSD CMP 2650.0\n0.5r\nSL : 2640.0")
    assert sig2["risk_multiplier"] == 0.5
    assert sig2["half_risk"] is True

    # 3. "half-risk"
    sig3 = parse_signal("BUY XAUUSD CMP 2650.0\nhalf-risk\nSL : 2640.0")
    assert sig3["risk_multiplier"] == 0.5
    assert sig3["half_risk"] is True

    # 4. "1/2 risk"
    sig4 = parse_signal("BUY XAUUSD CMP 2650.0\n1/2 risk\nSL : 2640.0")
    assert sig4["risk_multiplier"] == 0.5
    assert sig4["half_risk"] is True

    # 5. Normal signal without half risk
    sig5 = parse_signal("BUY XAUUSD CMP 2650.0\nSL : 2640.0")
    assert sig5["risk_multiplier"] == 1.0
    assert sig5["half_risk"] is False

def test_parse_provider_d_entry_cmp_default_xauusd():
    msg = "BUY CMP 4150 SL 4142.7"
    sig = parse_signal(msg)
    assert sig["type"] == SignalType.ENTRY
    assert sig["action"] == "BUY"
    assert sig["symbol"] == "XAUUSD"
    assert sig["price"] == 4150.0
    assert sig["sl"] == 4142.7

def test_parse_provider_d_sell_cmp():
    msg = "SELL CMP 4165.5\nSL: 4172.0"
    sig = parse_signal(msg)
    assert sig["type"] == SignalType.ENTRY
    assert sig["action"] == "SELL"
    assert sig["symbol"] == "XAUUSD"
    assert sig["price"] == 4165.5
    assert sig["sl"] == 4172.0

def test_parse_provider_d_breakeven_fast():
    msg = "SL BE fast @everyone"
    sig = parse_signal(msg)
    assert sig["type"] == SignalType.SL_UPDATE
    assert sig["symbol"] == "XAUUSD"
    assert sig.get("breakeven") is True

def test_parse_provider_d_put_your_sl_be():
    msg = "Put your SL BE fast"
    sig = parse_signal(msg)
    assert sig["type"] == SignalType.SL_UPDATE
    assert sig["symbol"] == "XAUUSD"
    assert sig.get("breakeven") is True

def test_parse_provider_d_loss_exit():
    msg = "Close in 10 pips loss @everyone"
    sig = parse_signal(msg)
    assert sig["type"] == SignalType.CLOSE_POSITION
    assert sig["symbol"] == "XAUUSD"

def test_parse_provider_d_exit_from_tarde_loss():
    msg = "Exit from the tarde in 10 pips loss"
    sig = parse_signal(msg)
    assert sig["type"] == SignalType.CLOSE_POSITION
    assert sig["symbol"] == "XAUUSD"



