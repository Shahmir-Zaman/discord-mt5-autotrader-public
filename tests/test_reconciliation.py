import sys
from unittest.mock import MagicMock, patch
import pytest

# Ensure MT5 is mocked before importing
mock_mt5 = MagicMock()
mock_mt5.TRADE_RETCODE_DONE = 10009
mock_mt5.initialize.return_value = True
sys.modules.setdefault("MetaTrader5", mock_mt5)

from web.service import BotService
from src.parser import SignalType

@pytest.fixture
def bot_service():
    svc = BotService.get_instance()
    svc.processed_ids.clear()
    svc.get_executor().adopted_tickets.clear()
    svc.get_executor().adopted_metadata.clear()
    return svc

def test_reconciliation_adopts_matching_open_position(bot_service):
    """An open MT5 position matching a Discord ENTRY signal is adopted."""
    mock_pos = {
        "ticket": 555111,
        "symbol": "XAUUSDm",
        "type": "BUY",
        "volume": 0.1,
        "price_open": 2650.0,
        "price_current": 2652.0,
        "sl": 0.0,
        "tp": 0.0,
        "profit": 20.0,
        "magic": 0,
        "is_pending": False,
        "is_bot_trade": False,
        "provider": "provider_a",
        "account": "provider_a (123)"
    }

    mock_msgs = [
        {"id": "disc-101", "text": "BUY XAUUSD CMP 2650.0"}
    ]

    with patch.object(bot_service, "get_open_positions", return_value=[mock_pos]), \
         patch("web.service.extract_recent_discord_messages", return_value=(mock_msgs, "channel-a")), \
         patch("web.service.log_trade_event") as mock_log, \
         patch("web.service.send_telegram_message") as mock_tg:
        
        res = bot_service.reconcile_and_adopt_trades()
        assert res["success"] is True
        assert res["adopted_count"] == 1
        assert 555111 in bot_service.get_executor().adopted_tickets
        assert "channel-a:disc-101" in bot_service.processed_ids
        mock_log.assert_called_once()
        assert mock_log.call_args[0][0] == "ADOPT"
        assert mock_log.call_args[0][5] == 555111

def test_reconciliation_replays_subsequent_sl_update(bot_service):
    """Subsequent SL updates in Discord are chronologically replayed on adopted trades."""
    mock_pos = {
        "ticket": 555222,
        "symbol": "XAUUSDm",
        "type": "BUY",
        "volume": 0.1,
        "price_open": 2650.0,
        "price_current": 2658.0,
        "sl": 2635.0,  # Old initial SL
        "tp": 0.0,
        "profit": 80.0,
        "magic": 0,
        "is_pending": False,
        "is_bot_trade": False,
        "provider": "provider_d",
        "account": "provider_d (123)"
    }

    mock_msgs = [
        {"id": "disc-entry", "text": "BUY XAUUSD CMP 2650.0 SL 2635.0"},
        {"id": "disc-sl-update", "text": "XAUUSD Set SL to 2645.0 (+100 pips lock)"}
    ]

    def mock_extract(channel_keyword=None, limit=25):
        if channel_keyword == "provider_d":
            return (mock_msgs, "provider_d")
        return ([], "")

    with patch.object(bot_service, "get_open_positions", return_value=[mock_pos]), \
         patch("web.service.extract_recent_discord_messages", side_effect=mock_extract), \
         patch.object(bot_service.get_executor(), "execute_signal", return_value="SL Updated to 2645.0") as mock_exec, \
         patch("web.service.log_trade_event"), \
         patch("web.service.send_telegram_message"):

        res = bot_service.reconcile_and_adopt_trades()
        assert res["success"] is True
        assert res["adopted_count"] == 1
        assert 555222 in bot_service.get_executor().adopted_tickets
        
        # Verify subsequent SL update was executed
        assert mock_exec.call_count == 1
        called_sig = mock_exec.call_args[0][0]
        assert called_sig["type"] == SignalType.SL_UPDATE
        assert called_sig["sl"] == 2645.0
        assert "provider_d:disc-sl-update" in bot_service.processed_ids

def test_reconciliation_ignores_unmatched_manual_positions(bot_service):
    """Manual trades that do not match any recent Discord ENTRY signals remain untouched."""
    mock_pos = {
        "ticket": 555333,
        "symbol": "EURUSD",
        "type": "SELL",
        "volume": 0.5,
        "price_open": 1.0850,
        "price_current": 1.0840,
        "sl": 1.0900,
        "tp": 1.0800,
        "profit": 50.0,
        "magic": 0,
        "is_pending": False,
        "is_bot_trade": False,
        "provider": "provider_a",
        "account": "provider_a (123)"
    }

    # Discord only has gold signals
    mock_msgs = [
        {"id": "disc-gold", "text": "BUY XAUUSD CMP 2650.0"}
    ]

    with patch.object(bot_service, "get_open_positions", return_value=[mock_pos]), \
         patch("web.service.extract_recent_discord_messages", return_value=(mock_msgs, "channel-a")), \
         patch("web.service.log_trade_event") as mock_log:

        res = bot_service.reconcile_and_adopt_trades()
        assert res["success"] is True
        assert res["adopted_count"] == 0
        assert 555333 not in bot_service.get_executor().adopted_tickets
        mock_log.assert_not_called()

def test_api_reconcile_trades_endpoint():
    """The /api/trades/reconcile HTTP POST endpoint triggers reconciliation."""
    from fastapi.testclient import TestClient
    from web.server import app

    client = TestClient(app)
    with patch("web.service.BotService.reconcile_and_adopt_trades", return_value={"success": True, "adopted_count": 0, "adopted": []}) as mock_rec:
        resp = client.post("/api/trades/reconcile")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        mock_rec.assert_called_once()

def test_manually_adopt_trade(bot_service):
    """manually_adopt_trade adopts an open position for a specified channel."""
    mock_pos = {
        "ticket": 777888,
        "symbol": "XAUUSDm",
        "type": "BUY",
        "volume": 0.05,
        "price_open": 4172.5,
        "price_current": 4173.0,
        "profit": 25.0,
        "is_pending": False,
        "provider": "provider_a"
    }

    with patch.object(bot_service, "get_open_positions", return_value=[mock_pos]), \
         patch("web.service.log_trade_event") as mock_log, \
         patch("web.service.send_telegram_message") as mock_tg:

        res = bot_service.manually_adopt_trade(777888, "channel-a")
        assert res["success"] is True
        assert res["ticket"] == 777888
        assert res["channel"] == "channel-a"
        assert 777888 in bot_service.get_executor().adopted_tickets
        assert bot_service.get_executor().is_ticket_managed(777888, 0) is True
        mock_log.assert_called_once()
        assert mock_log.call_args[0][0] == "ADOPT"

def test_manually_unadopt_trade(bot_service):
    """manually_unadopt_trade releases a previously adopted trade."""
    bot_service.get_executor().adopt_ticket(999111, symbol="XAUUSDm", channel="channel-a")
    assert 999111 in bot_service.get_executor().adopted_tickets

    with patch("web.service.log_trade_event") as mock_log, \
         patch("web.service.send_telegram_message") as mock_tg:

        res = bot_service.manually_unadopt_trade(999111)
        assert res["success"] is True
        assert 999111 not in bot_service.get_executor().adopted_tickets
        assert bot_service.get_executor().is_ticket_managed(999111, 0) is False
        mock_log.assert_called_once()
        assert mock_log.call_args[0][0] == "UNADOPT"

def test_api_manual_adopt_and_unadopt_endpoints():
    """HTTP endpoints /api/trades/adopt and /api/trades/unadopt/{ticket} correctly call service."""
    from fastapi.testclient import TestClient
    from web.server import app

    client = TestClient(app)
    with patch("web.service.BotService.manually_adopt_trade", return_value={"success": True, "ticket": 12345, "channel": "channel-a"}) as mock_adopt, \
         patch("web.service.BotService.manually_unadopt_trade", return_value={"success": True, "ticket": 12345}) as mock_unadopt:

        # Test POST /api/trades/adopt
        resp = client.post("/api/trades/adopt", json={"ticket": 12345, "channel": "channel-a", "symbol": "XAUUSDm"})
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        mock_adopt.assert_called_once_with(12345, "channel-a", "XAUUSDm")

        # Test POST /api/trades/unadopt/12345
        resp_un = client.post("/api/trades/unadopt/12345")
        assert resp_un.status_code == 200
        assert resp_un.json()["success"] is True
        mock_unadopt.assert_called_once_with(12345)

def test_handle_notification_executes_close_with_followup_chat(bot_service):
    """When a close signal is followed by conversational chat, both are scanned and the close executes."""
    data = {"title": "example_provider_a", "message": "New notification from example_provider_a."}
    close_text = "@everyone @Forex Alert\nXAUUSD Trade Closed (4182.7)\nResult: -64 pips"
    chat_text = "Will enter again from bottom"
    
    msgs = [
        {"id": "msg-close-1", "text": close_text},
        {"id": "msg-chat-1", "text": chat_text}
    ]
    
    with patch("web.service.extract_recent_discord_messages", return_value=(msgs, "channel-a")), \
         patch.object(bot_service.get_executor(), "execute_signal", return_value="Position closed: XAUUSD") as mock_exec, \
         patch("web.service.send_telegram_message"):
        
        bot_service._handle_notification(data, "token", "chat_id", 0.01)
        assert mock_exec.call_count == 1
        sig = mock_exec.call_args[0][0]
        assert sig["type"] == SignalType.CLOSE_POSITION
        assert sig["symbol"] == "XAUUSD"
        assert "channel-a:msg-close-1" in bot_service.processed_ids
        assert "channel-a:msg-chat-1" in bot_service.processed_ids


