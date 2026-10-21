import os
import sys
from unittest.mock import MagicMock, patch

# Ensure mocks for MT5 and Playwright
mock_mt5 = MagicMock()
mock_mt5.initialize.return_value = True
mock_mt5.TRADE_RETCODE_DONE = 10009
mock_mt5.ORDER_TYPE_BUY = 0
mock_mt5.ORDER_TYPE_SELL = 1
mock_mt5.TRADE_ACTION_DEAL = 1
mock_mt5.ORDER_TIME_GTC = 0
mock_mt5.ORDER_FILLING_IOC = 2
mock_mt5.SYMBOL_FILLING_IOC = 2

mock_mt5.account_info.return_value = MagicMock(
    login=123456,
    server="Demo-Server",
    balance=10000.0,
    equity=10050.0,
    profit=50.0,
    margin_free=9900.0,
    leverage=100,
    currency="USD"
)
mock_mt5.positions_get.return_value = []
sys.modules.setdefault("MetaTrader5", mock_mt5)
sys.modules.setdefault("playwright", MagicMock())
sys.modules.setdefault("playwright.sync_api", MagicMock())

from fastapi.testclient import TestClient
from web.server import app

client = TestClient(app)

def test_api_status():
    response = client.get("/api/status")
    assert response.status_code == 200
    data = response.json()
    assert "bot_running" in data
    assert "trigger_mode" in data
    assert "mt5" in data
    assert "cdp" in data

def test_api_mode_switch():
    response = client.post("/api/bot/mode", json={"mode": "polling"})
    assert response.status_code == 200
    assert response.json()["trigger_mode"] == "polling"

    response = client.post("/api/bot/mode", json={"mode": "ntfy"})
    assert response.status_code == 200
    assert response.json()["trigger_mode"] == "ntfy"

def test_api_mock_trigger():
    # Setup mock return values explicitly for this test
    mt5_module = sys.modules["MetaTrader5"]
    mock_tick = MagicMock()
    mock_tick.ask = 4526.5
    mock_tick.bid = 4526.0
    mt5_module.symbol_info_tick.return_value = mock_tick
    mt5_module.symbol_select.return_value = True
    mt5_module.symbol_info.return_value = MagicMock(filling_mode=2)
    mt5_module.order_send.return_value = MagicMock(retcode=10009, order=12345)

    with patch("web.service.extract_discord_message", return_value=("msg-123", "BUY XAUUSD CMP 4526.5", "channel-a")), \
         patch("web.service.send_telegram_message"):
        response = client.post("/api/test/mock-trigger", json={"channel": "channel-a"})
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["channel"] == "channel-a"
        assert "12345" in data["execution_result"]

def test_api_positions():
    response = client.get("/api/positions")
    assert response.status_code == 200
    assert isinstance(response.json(), list)

def test_api_config_get():
    response = client.get("/api/config")
    assert response.status_code == 200
    data = response.json()
    assert "PROVIDER_A_FIXED_LOT" in data or "NTFY_TOPIC" in data
    assert "PROVIDER_A_SYMBOL_SUFFIX" in data
    assert "PROVIDER_B_SYMBOL_SUFFIX" in data

def test_api_config_per_terminal_suffix():
    cfg = client.get("/api/config").json()
    cfg["PROVIDER_A_SYMBOL_SUFFIX"] = "m"
    cfg["PROVIDER_B_SYMBOL_SUFFIX"] = "c"
    res = client.post("/api/config", json=cfg)
    assert res.status_code == 200
    data = res.json()["config"]
    assert data["PROVIDER_A_SYMBOL_SUFFIX"] == "m"
    assert data["PROVIDER_B_SYMBOL_SUFFIX"] == "c"

def test_api_simulate_signal():
    payload = {
        "text": "@provider_b @Forex Alert\nBUY XAUUSD CMP 4526.5\n\nSL : 4520.0 (-65 pips)",
        "execute": False
    }
    response = client.post("/api/test/simulate-signal", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["parsed"]["type"] == "ENTRY"
    assert data["parsed"]["symbol"] == "XAUUSD"
    assert data["parsed"]["price"] == 4526.5
    assert data["parsed"]["sl"] == 4520.0
    assert data["executed"] is False

def test_api_index_html():
    response = client.get("/")
    assert response.status_code == 200
    assert "AutoTrader" in response.text

def test_api_events():
    response = client.get("/api/events")
    assert response.status_code == 200
    assert isinstance(response.json(), list)

def test_api_clear_history(tmp_path, monkeypatch):
    test_csv = tmp_path / "trades_journal.csv"
    test_csv.write_text("Timestamp,Provider,EventType,Symbol,Action,Price,Volume,Ticket,Profit,Notes\n2026-09-03 12:00:00,provider_a,OPEN,XAUUSD,BUY,4500,0.01,111,0,test\n")
    monkeypatch.setattr("os.getcwd", lambda: str(tmp_path))
    
    response = client.post("/api/history/clear")
    assert response.status_code == 200
    assert response.json()["success"] is True
    
    content = test_csv.read_text().strip().splitlines()
    assert len(content) == 1
    assert "Timestamp,Provider,EventType" in content[0]

def test_api_terminals_status():
    response = client.get("/api/terminals/status")
    assert response.status_code == 200
    data = response.json()
    assert "provider_a" in data
    assert "provider_b" in data
    assert "both_running" in data
    assert "any_running" in data

def test_api_terminals_launch():
    with patch("web.service.launch_terminal_by_path", return_value={"success": True, "already_running": False, "message": "Terminal launched"}):
        response = client.post("/api/terminals/launch", json={"terminal": "provider_a"})
        assert response.status_code == 200
        data = response.json()
        assert "launched" in data or "already_running" in data

def test_api_simulate_signal_with_channel():
    payload = {
        "text": "PLACE XAUUSD BUY LIMIT\n\nEntry : 4470.1 | 4463.5\nSL : 4457.1 (-130 pips)\n\nManage Risk",
        "channel": "channel-b",
        "execute": False
    }
    response = client.post("/api/test/simulate-signal", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["parsed"]["type"] == "ENTRY"
    assert data["parsed"]["price"] == 4470.1
    assert data["parsed"]["price2"] == 4463.5
    assert data["channel"] == "channel-b"

def test_api_active_trader_switch():
    # Test switching to provider_a
    res = client.post("/api/bot/trader", json={"trader": "provider_a"})
    assert res.status_code == 200
    assert res.json()["active_trader"] == "provider_a"

    status_res = client.get("/api/status")
    assert status_res.status_code == 200
    assert status_res.json()["active_trader"] == "provider_a"

    # Test switching to provider_b
    res = client.post("/api/bot/trader", json={"trader": "provider_b"})
    assert res.status_code == 200
    assert res.json()["active_trader"] == "provider_b"

    # Test switching to provider_c
    res = client.post("/api/bot/trader", json={"trader": "provider_c"})
    assert res.status_code == 200
    assert res.json()["active_trader"] == "provider_c"

    status_res = client.get("/api/status")
    assert status_res.status_code == 200
    assert status_res.json()["active_trader"] == "provider_c"

    # Test switching to all
    res = client.post("/api/bot/trader", json={"trader": "all"})
    assert res.status_code == 200
    assert res.json()["active_trader"] == "all"

    # Test invalid trader rejected
    res = client.post("/api/bot/trader", json={"trader": "unknown_trader"})
    assert res.status_code == 400

def test_api_mock_trigger_provider_c():
    mt5_module = sys.modules["MetaTrader5"]
    mock_tick = MagicMock()
    mock_tick.ask = 2640.0
    mock_tick.bid = 2639.5
    mt5_module.symbol_info_tick.return_value = mock_tick
    mt5_module.symbol_select.return_value = True
    mt5_module.symbol_info.return_value = MagicMock(filling_mode=2)
    mt5_module.order_send.return_value = MagicMock(retcode=10009, order=88888)

    with patch("web.service.extract_discord_message", return_value=("msg-999", "BUY XAUUSD CMP 2640.0\nSL: 2634.0", "channel-c")), \
         patch("web.service.send_telegram_message"):
        response = client.post("/api/test/mock-trigger", json={"channel": "channel-c"})
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["channel"] == "channel-c"
        assert "88888" in data["execution_result"]

def test_api_bot_start_with_trader():
    # Test starting bot with trader payload
    res = client.post("/api/bot/start", json={"trader": "provider_c"})
    assert res.status_code == 200
    
    status_res = client.get("/api/status")
    assert status_res.json()["active_trader"] == "provider_c"

    # Stop bot and reset
    client.post("/api/bot/stop")
    client.post("/api/bot/trader", json={"trader": "all"})

def test_find_available_port_auto_increment():
    import socket
    from app import is_port_in_use, find_available_port, get_requested_port

    # Test port in use detection with a temporary socket
    temp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    temp_sock.bind(("0.0.0.0", 9444))
    temp_sock.listen(1)

    try:
        assert is_port_in_use(9444) is True
        # Since 9444 is in use, find_available_port starting at 9444 must return 9445
        available = find_available_port(start_port=9444)
        assert available == 9445
    finally:
        temp_sock.close()

    # After closing, 9444 is immediately available
    assert is_port_in_use(9444) is False
    assert find_available_port(start_port=9444) == 9444

def test_get_requested_port_parsing(monkeypatch):
    import sys
    from app import get_requested_port

    monkeypatch.setattr(sys, "argv", ["app.py", "8050"])
    assert get_requested_port() == 8050

    monkeypatch.setattr(sys, "argv", ["app.py", "--port", "8090"])
    assert get_requested_port() == 8090

    monkeypatch.setattr(sys, "argv", ["app.py", "-p", "8100"])
    assert get_requested_port() == 8100

    monkeypatch.setattr(sys, "argv", ["app.py"])
    monkeypatch.setenv("PORT", "9000")
    assert get_requested_port() == 9000

def test_api_instance_info():
    res = client.get("/api/instance/info")
    assert res.status_code == 200
    data = res.json()
    assert "port" in data
    assert "instance_id" in data
    assert "target_terminal" in data
    assert "terminals" in data

def test_api_set_bot_terminal():
    # 1. Switch to terminal_1
    res = client.post("/api/bot/terminal", json={"target": "terminal_1"})
    assert res.status_code == 200
    assert res.json()["target_terminal"] == "terminal_1"

    status_res = client.get("/api/status")
    assert status_res.json()["target_terminal"] == "terminal_1"

    # 2. Switch to terminal_2
    res2 = client.post("/api/bot/terminal", json={"target": "terminal_2"})
    assert res2.status_code == 200
    assert res2.json()["target_terminal"] == "terminal_2"

    # 3. Switch to terminal_3
    res3 = client.post("/api/bot/terminal", json={"target": "terminal_3"})
    assert res3.status_code == 200
    assert res3.json()["target_terminal"] == "terminal_3"

    # 4. Reset to auto
    res4 = client.post("/api/bot/terminal", json={"target": "auto"})
    assert res4.status_code == 200
    assert res4.json()["target_terminal"] == "auto"

def test_instance_config_isolation(tmp_path, monkeypatch):
    import json
    from web.service import BotService

    # Set mock port 8005
    monkeypatch.setenv("AUTOTRADER_PORT", "8005")
    srv = BotService.get_instance()
    srv.port = "8005"
    
    # Save a setting for port 8005
    srv.save_config({"TARGET_TERMINAL": "terminal_2", "PROVIDER_A_FIXED_LOT": "0.05"})
    
    # Check that config_8005.json was written
    cfg_file = os.path.join(os.getcwd(), "config_8005.json")
    try:
        assert os.path.exists(cfg_file)
        with open(cfg_file, "r") as f:
            saved = json.load(f)
        assert saved.get("TARGET_TERMINAL") == "terminal_2"
        assert saved.get("PROVIDER_A_FIXED_LOT") == "0.05"
        
        # Verify get_config() includes the override
        loaded = srv.get_config()
        assert loaded.get("TARGET_TERMINAL") == "terminal_2"
        assert loaded.get("PROVIDER_A_FIXED_LOT") == "0.05"
    finally:
        if os.path.exists(cfg_file):
            os.remove(cfg_file)
        # Restore port
        srv.port = "8000"
        monkeypatch.delenv("AUTOTRADER_PORT", raising=False)

def test_journal_file_port_isolation(monkeypatch):
    from src.journal import get_journal_file, JOURNAL_FILE
    
    # Ensure default is trades_journal.csv
    monkeypatch.delenv("AUTOTRADER_PORT", raising=False)
    monkeypatch.setattr("src.journal.JOURNAL_FILE", "trades_journal.csv")
    assert get_journal_file() == "trades_journal.csv"

    # Port 8000 uses trades_journal.csv
    monkeypatch.setenv("AUTOTRADER_PORT", "8000")
    assert get_journal_file() == "trades_journal.csv"

    # Port 8001 uses trades_journal_8001.csv
    monkeypatch.setenv("AUTOTRADER_PORT", "8001")
    assert get_journal_file() == "trades_journal_8001.csv"

def test_extension_heartbeat_and_status():
    """Verify Chrome extension heartbeat updates status."""
    res = client.post("/api/extension/heartbeat", json={
        "channel": "channel-a",
        "channel_id": "123456789012345678",
        "url": "https://discord.com/channels/123/123456789012345678"
    })
    assert res.status_code == 200
    assert res.json()["success"] is True

    status_res = client.get("/api/extension/status")
    assert status_res.status_code == 200
    st = status_res.json()
    assert st["connected"] is True
    assert st["channel"] == "channel-a"

    # Also verify /api/status includes extension info
    all_status = client.get("/api/status").json()
    assert "extension" in all_status
    assert all_status["extension"]["connected"] is True

def test_extension_message_execution():
    """Verify signal pushed from Chrome Extension is parsed and executed."""
    import time
    # Set mock return for tick & order_send
    mt5_module = sys.modules["MetaTrader5"]
    mock_tick = MagicMock(ask=2650.0, bid=2649.5)
    mt5_module.symbol_info_tick.return_value = mock_tick
    mock_order = MagicMock(retcode=10009, order=888999)
    mt5_module.order_send.return_value = mock_order

    msg_id = f"chat-messages-{int(time.time() * 1000) << 22}"
    payload = {
        "channel": "channel-a",
        "channel_id": "123456789012345678",
        "message_id": msg_id,
        "text": "BUY XAUUSD CMP 2650.0\nSL 2640.0\nTP 2670.0",
        "author": "example_provider_a",
        "timestamp": time.time() * 1000
    }

    res = client.post("/api/extension/message", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["success"] is True
    assert data["status"] == "executed"
    assert data["channel"] == "channel-a"
    assert data["signal"]["action"] == "BUY"

    # Duplicate send with same ID should be deduped
    dup_res = client.post("/api/extension/message", json=payload)
    assert dup_res.status_code == 200
    assert dup_res.json()["status"] == "duplicate"

def test_extension_message_filtered_by_active_trader():
    """Verify signals from channels not matching active trader are ignored."""
    from web.service import BotService
    srv = BotService.get_instance()
    srv.active_trader = "provider_a"

    payload = {
        "channel": "channel-b",
        "channel_id": "123456789012345678",
        "message_id": "chat-messages-999999999",
        "text": "BUY XAUUSD CMP 2650.0",
        "author": "provider_b",
        "timestamp": 0
    }

    res = client.post("/api/extension/message", json=payload)
    assert res.status_code == 200
    assert res.json()["status"] == "filtered"
    srv.active_trader = "all"

def test_api_test_telegram_error():
    with patch("web.server.send_telegram_error", return_value=True):
        res = client.post("/api/test/telegram-error", json={"token": "t", "chat_id": "c"})
        assert res.status_code == 200
        assert res.json()["success"] is True






