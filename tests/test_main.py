import sys
from unittest.mock import MagicMock, patch, ANY

# Mock modules that might not be installed in the test environment
sys.modules.setdefault("playwright", MagicMock())
sys.modules.setdefault("playwright.sync_api", MagicMock())
sys.modules.setdefault("MetaTrader5", MagicMock())

from main import on_notification, _processed_ids
from src.parser import SignalType

# Create a mock executor for tests
def _make_mock_executor():
    return MagicMock()

def setup_function():
    """Clear processed IDs between tests."""
    _processed_ids.clear()

def test_on_notification_unapproved_sender():
    executor = _make_mock_executor()
    unapproved_data = {"title": "#mohid-fx - Mohid", "message": "General > #mohid-fx\nMohid"}
    with patch("main.extract_recent_discord_messages") as mock_extract:
        on_notification(unapproved_data, executor, "token", "chat_id", 0.01)
        mock_extract.assert_not_called()
        executor.execute_signal.assert_not_called()

def test_on_notification_no_text():
    executor = _make_mock_executor()
    provider_a_data = {"title": "#channel-a - example_provider_a", "message": "General > #channel-a\nexample_provider_a"}
    with patch("main.extract_recent_discord_messages", return_value=([], "channel-a")), \
         patch("main.extract_discord_message", return_value=("", "", "channel-a")):
        on_notification(provider_a_data, executor, "token", "chat_id", 0.01)
        executor.execute_signal.assert_not_called()

def test_on_notification_unknown_signal():
    executor = _make_mock_executor()
    provider_a_data = {"title": "#channel-a - example_provider_a", "message": "General > #channel-a\nexample_provider_a"}
    with patch("main.extract_recent_discord_messages", return_value=([{"id": "msg-1", "text": "Hello world"}], "channel-a")), \
         patch("main.parse_signal", return_value={"type": SignalType.UNKNOWN}):
        on_notification(provider_a_data, executor, "token", "chat_id", 0.01)
        executor.execute_signal.assert_not_called()

def test_on_notification_valid_provider_a_signal():
    executor = _make_mock_executor()
    provider_a_data = {"title": "#channel-a - example_provider_a", "message": "General > #channel-a\nexample_provider_a"}
    signal = {"type": SignalType.ENTRY, "action": "BUY", "symbol": "XAUUSD", "price": 4625.9}
    
    with patch("main.extract_recent_discord_messages", return_value=([{"id": "msg-2", "text": "BUY XAUUSD CMP 4625.9"}], "channel-a")) as mock_extract, \
         patch("main.parse_signal", return_value=signal), \
         patch("main.send_telegram_message") as mock_send:
        executor.execute_signal.return_value = "Trade opened: BUY XAUUSD at 4625.9 (Ticket: 123)"
        on_notification(provider_a_data, executor, "token", "chat_id", 0.01)
        
        mock_extract.assert_called_once_with(channel_keyword="channel-a", limit=10)
        executor.execute_signal.assert_called_once_with(signal, channel="channel-a", config=ANY)
        mock_send.assert_called_once_with("token", "chat_id", "🔔 [#channel-a]\nTrade opened: BUY XAUUSD at 4625.9 (Ticket: 123)")

def test_on_notification_valid_provider_b_signal():
    executor = _make_mock_executor()
    provider_b_data = {"title": "#channel-b - ! provider_b Example", "message": "General > #channel-b\n! provider_b Example"}
    signal = {"type": SignalType.ENTRY, "action": "BUY", "symbol": "XAUUSD", "price": 4526.5, "sl": 4520.0}
    
    with patch("main.extract_recent_discord_messages", return_value=([{"id": "msg-provider_b-1", "text": "BUY XAUUSD CMP 4526.5\nSL: 4520.0"}], "channel-b")) as mock_extract, \
         patch("main.parse_signal", return_value=signal), \
         patch("main.send_telegram_message") as mock_send:
        executor.execute_signal.return_value = "Trade opened: BUY XAUUSD at 4526.5 (SL: 4520.0) (Ticket: 456)"
        on_notification(provider_b_data, executor, "token", "chat_id", 0.02)
        
        mock_extract.assert_called_once_with(channel_keyword="channel-b", limit=10)
        executor.execute_signal.assert_called_once_with(signal, channel="channel-b", config=ANY)
        mock_send.assert_called_once_with("token", "chat_id", "🔔 [#channel-b]\nTrade opened: BUY XAUUSD at 4526.5 (SL: 4520.0) (Ticket: 456)")

def test_on_notification_sl_update():
    executor = _make_mock_executor()
    provider_b_data = {"title": "#channel-b - ! provider_b Example", "message": "General > #channel-b"}
    signal = {"type": SignalType.SL_UPDATE, "symbol": "XAUUSD", "sl": 4526.5}
    with patch("main.extract_recent_discord_messages", return_value=([{"id": "msg-3", "text": "XAUUSD Move SL to Breakeven (4526.5)"}], "channel-b")), \
         patch("main.parse_signal", return_value=signal), \
         patch("main.send_telegram_message") as mock_send:
        executor.execute_signal.return_value = "SL Updated for XAUUSD to 4526.5"
        on_notification(provider_b_data, executor, "token", "chat_id", 0.01)
        executor.execute_signal.assert_called_once_with(signal, channel="channel-b", config=ANY)
        mock_send.assert_called_once_with("token", "chat_id", "🔔 [#channel-b]\nSL Updated for XAUUSD to 4526.5")

def test_deduplication_prevents_duplicate_trades():
    executor = _make_mock_executor()
    provider_a_data = {"title": "#channel-a - example_provider_a", "message": "General > #channel-a"}
    signal = {"type": SignalType.ENTRY, "action": "BUY", "symbol": "XAUUSD", "price": 2000.0}
    with patch("main.extract_recent_discord_messages", return_value=([{"id": "msg-dup", "text": "BUY XAUUSD CMP 2000.0"}], "channel-a")), \
         patch("main.parse_signal", return_value=signal), \
         patch("main.send_telegram_message"):
        executor.execute_signal.return_value = "Trade opened"
        # First call should execute
        on_notification(provider_a_data, executor, "token", "chat_id", 0.01)
        assert executor.execute_signal.call_count == 1
        # Second call with same message ID should be skipped
        on_notification(provider_a_data, executor, "token", "chat_id", 0.01)
        assert executor.execute_signal.call_count == 1  # Still 1, not 2

def test_on_notification_multi_message_close_then_chat():
    executor = _make_mock_executor()
    provider_a_data = {"title": "example_provider_a", "message": "New notification from example_provider_a."}
    close_text = "@everyone @Forex Alert\nXAUUSD Trade Closed (4182.7)\nResult: -64 pips"
    chat_text = "Will enter again from bottom"
    
    msgs = [
        {"id": "msg-close", "text": close_text},
        {"id": "msg-chat", "text": chat_text}
    ]
    
    with patch("main.extract_recent_discord_messages", return_value=(msgs, "channel-a")), \
         patch("main.send_telegram_message"):
        executor.execute_signal.return_value = "Position closed: XAUUSD at 4182.7"
        on_notification(provider_a_data, executor, "token", "chat_id", 0.01)
        
        # Only the close signal should have been executed; chat is UNKNOWN and ignored
        assert executor.execute_signal.call_count == 1
        call_signal = executor.execute_signal.call_args[0][0]
        assert call_signal["type"] == SignalType.CLOSE_POSITION
        assert call_signal["symbol"] == "XAUUSD"

def test_on_notification_invalid_message_is_deduped():
    """Messages failing validation (e.g. too old) should be added to _processed_ids to prevent re-processing."""
    executor = _make_mock_executor()
    provider_a_data = {"title": "example_provider_a", "message": "Notification"}
    signal = {"type": SignalType.ENTRY, "action": "BUY", "symbol": "XAUUSD", "price": 2650.0}
    
    with patch("main.extract_recent_discord_messages", return_value=([{"id": "msg-old-1", "text": "BUY XAUUSD CMP 2650.0"}], "channel-a")), \
         patch("main.parse_signal", return_value=signal), \
         patch("main.is_message_valid", return_value=False):
        
        on_notification(provider_a_data, executor, "token", "chat_id", 0.01)
        assert executor.execute_signal.call_count == 0
        assert "channel-a:msg-old-1" in _processed_ids

