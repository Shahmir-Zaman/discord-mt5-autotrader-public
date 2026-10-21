import os
import sys
import logging
from typing import Dict, Any, Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from web.service import BotService
from src.notifier import send_telegram_message, send_telegram_error, setup_telegram_error_logging

setup_telegram_error_logging()

app = FastAPI(title="Discord-MT5 AutoTrader Dashboard")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
service = BotService.get_instance()

class ConfigPayload(BaseModel):
    model_config = {"extra": "allow"}

    NTFY_TOPIC: Optional[str] = ""
    TG_TOKEN: Optional[str] = ""
    TG_CHAT_ID: Optional[str] = ""
    SYMBOL_SUFFIX: Optional[str] = ""
    PROVIDER_A_SYMBOL_SUFFIX: Optional[str] = ""
    PROVIDER_B_SYMBOL_SUFFIX: Optional[str] = ""
    TERMINAL_OWNER_SYMBOL_SUFFIX: Optional[str] = ""
    PROVIDER_A_LOT_MODE: Optional[str] = "FIXED"
    PROVIDER_A_FIXED_LOT: Optional[str] = "0.01"
    PROVIDER_A_RISK_PCT: Optional[str] = "1.0"
    PROVIDER_B_LOT_MODE: Optional[str] = "FIXED"
    PROVIDER_B_FIXED_LOT: Optional[str] = "0.01"
    PROVIDER_B_RISK_PCT: Optional[str] = "1.0"
    PROVIDER_B_DUAL_ENTRY_SPLIT: Optional[str] = "75/25"
    PROVIDER_C_LOT_MODE: Optional[str] = "FIXED"
    PROVIDER_C_FIXED_LOT: Optional[str] = "0.01"
    PROVIDER_C_RISK_PCT: Optional[str] = "1.0"
    PROVIDER_D_TARGET_TERMINAL: Optional[str] = "terminal_1"
    PROVIDER_D_LOT_MODE: Optional[str] = "FIXED"
    PROVIDER_D_FIXED_LOT: Optional[str] = "0.01"
    PROVIDER_D_RISK_PCT: Optional[str] = "1.0"
    PROVIDER_D_SYMBOL_SUFFIX: Optional[str] = ""
    PROVIDER_D_STARTING_MILESTONE: Optional[str] = "100.0"
    TERMINAL_1_LOT_MODE: Optional[str] = "FIXED"
    TERMINAL_1_FIXED_LOT: Optional[str] = "0.12"
    TERMINAL_1_RISK_PCT: Optional[str] = "1.0"
    TERMINAL_2_LOT_MODE: Optional[str] = "FIXED"
    TERMINAL_2_FIXED_LOT: Optional[str] = "0.01"
    TERMINAL_2_RISK_PCT: Optional[str] = "1.0"
    TERMINAL_3_LOT_MODE: Optional[str] = "RISK"
    TERMINAL_3_FIXED_LOT: Optional[str] = "0.01"
    TERMINAL_3_RISK_PCT: Optional[str] = "1.0"
    TERMINAL_OWNER_LOT_MODE: Optional[str] = "RISK"
    TERMINAL_OWNER_FIXED_LOT: Optional[str] = "0.01"
    TERMINAL_OWNER_RISK_PCT: Optional[str] = "1.0"
    PROVIDER_A_MT5_PATH: Optional[str] = ""
    PROVIDER_B_MT5_PATH: Optional[str] = ""
    TERMINAL_OWNER_MT5_PATH: Optional[str] = ""
    HEARTBEAT_PING_URL: Optional[str] = ""

class TraderPayload(BaseModel):
    trader: str  # "provider_a", "provider_b", "provider_c", "provider_d", or "all"

class ModePayload(BaseModel):
    mode: str  # "polling" or "ntfy"

class TerminalPayload(BaseModel):
    target: str  # "terminal_1", "terminal_2", "auto", or custom path

class LaunchTerminalPayload(BaseModel):
    terminal: Optional[str] = "all"  # "provider_a", "provider_b", or "all"

class MockTriggerPayload(BaseModel):
    channel: str  # "channel-a", "channel-b", "channel-c", or "provider_d"
    text: Optional[str] = None

class SimulateSignalPayload(BaseModel):
    text: str
    channel: Optional[str] = "channel-a"
    execute: bool = False
    lot_size: Optional[float] = 0.01

class TelegramTestPayload(BaseModel):
    token: Optional[str] = None
    chat_id: Optional[str] = None

class AdoptTradePayload(BaseModel):
    ticket: int
    channel: str
    symbol: Optional[str] = None

class ExtensionMessagePayload(BaseModel):
    channel: Optional[str] = ""
    channel_id: Optional[str] = ""
    message_id: str
    text: str
    author: Optional[str] = ""
    timestamp: Optional[float] = None

class ExtensionHeartbeatPayload(BaseModel):
    channel: Optional[str] = ""
    channel_id: Optional[str] = ""
    url: Optional[str] = ""

@app.get("/api/status")
def get_system_status():
    mt5_info = service.get_mt5_account_info()
    cdp_info = service.check_cdp_status()
    config = service.get_config()
    instance_info = service.get_instance_info()
    
    return {
        "bot_running": service.is_running,
        "trigger_mode": service.trigger_mode,
        "active_trader": getattr(service, "active_trader", "all"),
        "target_terminal": service.get_target_terminal(),
        "instance": instance_info,
        "mt5": mt5_info,
        "cdp": cdp_info,
        "extension": service.get_extension_status(),
        "active_triggers": service.get_active_breakeven_triggers(),
        "adopted_positions": service.get_executor().get_adopted_tickets(),
        "config_configured": bool(config.get("TG_TOKEN") and config.get("TG_CHAT_ID"))
    }

@app.post("/api/extension/message")
def receive_extension_message(payload: ExtensionMessagePayload):
    """Direct webhook endpoint for real-time Discord signals pushed from Chrome Extension."""
    return service.handle_extension_message(payload.model_dump())

@app.post("/api/extension/heartbeat")
def receive_extension_heartbeat(payload: ExtensionHeartbeatPayload):
    """Heartbeat endpoint pinged by Chrome Extension when Discord tab is active."""
    service.record_extension_heartbeat(channel=payload.channel or "", channel_id=payload.channel_id or "")
    cfg = service.get_config()
    return {
        "success": True,
        "active": True,
        "tg_token": cfg.get("TG_TOKEN", ""),
        "tg_chat_id": cfg.get("TG_CHAT_ID", ""),
        "port": service.port
    }

@app.get("/api/extension/status")
def get_extension_status():
    """Returns the live connection status of the Chrome Extension."""
    return service.get_extension_status()

@app.get("/api/instance/info")
def get_instance_info():
    return service.get_instance_info()

@app.post("/api/bot/terminal")
def set_bot_terminal(payload: TerminalPayload):
    res = service.set_target_terminal(payload.target)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("message", "Failed to set target terminal"))
    return res

@app.post("/api/bot/trader")
def set_bot_trader(payload: TraderPayload):
    res = service.set_active_trader(payload.trader)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("message", "Invalid trader target"))
    return res

@app.post("/api/bot/mode")
def set_bot_mode(payload: ModePayload):
    res = service.set_trigger_mode(payload.mode)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("message", "Invalid mode"))
    return res

@app.post("/api/bot/start")
def start_bot(payload: Optional[TraderPayload] = None):
    if payload and payload.trader:
        service.set_active_trader(payload.trader)
    res = service.start_bot()
    if not res["success"]:
        raise HTTPException(status_code=400, detail=res["message"])
    return res

@app.post("/api/bot/stop")
def stop_bot():
    res = service.stop_bot()
    if not res["success"]:
        raise HTTPException(status_code=400, detail=res["message"])
    return res

@app.get("/api/positions")
def get_positions():
    return service.get_open_positions()

@app.post("/api/positions/close/{ticket}")
def close_position(ticket: int):
    res = service.close_position_by_ticket(ticket)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("error", "Close failed"))
    return res

@app.get("/api/logs")
def get_logs():
    return {"logs": list(service.logs)}

@app.get("/api/history")
def get_history():
    import csv
    import os
    import re
    
    # Auto-sync any closed positions from MT5 (SL/TP or manual)
    try:
        from src.journal import sync_journal_with_mt5
        sync_journal_with_mt5()
    except Exception as e:
        logging.debug(f"Auto-sync error: {e}")

    from src.journal import get_journal_file
    journal_path = os.path.join(os.getcwd(), get_journal_file())
    
    if not os.path.exists(journal_path):
        return []
    
    try:
        rows = []
        with open(journal_path, mode='r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
    except Exception as e:
        logging.error(f"Failed to read history: {e}")
        return []
    
    # Aggregate events by ticket into trade objects
    trades_by_ticket = {}
    
    for row in rows:
        event = row.get("EventType", "")
        ticket = row.get("Ticket", "")
        
        # Skip test rows and rows with mock data
        if not ticket or "MagicMock" in str(row.get("Symbol", "")) or "MagicMock" in str(row.get("Profit", "")):
            continue
        if event == "TEST":
            continue
            
        if event in ("OPEN", "ADOPT"):
            action_raw = row.get("Action", "").upper()
            signal = "Long" if action_raw == "BUY" else ("Short" if action_raw == "SELL" else action_raw)
            
            # Extract SL/TP from notes if present (e.g. "(SL: 4520.0) (TP: 4540.0)" or "SL: 4520.0 TP: 4540.0")
            notes = row.get("Notes", "")
            sl_val = None
            tp_val = None
            sl_m = re.search(r'SL[:\s]+([0-9.]+)', notes)
            tp_m = re.search(r'TP[:\s]+([0-9.]+)', notes)
            if sl_m:
                sl_val = float(sl_m.group(1))
            if tp_m:
                tp_val = float(tp_m.group(1))
            
            try:
                entry_price = float(row.get("Price", 0))
            except (ValueError, TypeError):
                entry_price = 0.0
            try:
                volume = float(row.get("Volume", 0))
            except (ValueError, TypeError):
                volume = 0.0
                
            trades_by_ticket[ticket] = {
                "ticket": ticket,
                "symbol": row.get("Symbol", ""),
                "signal": signal,
                "lot_size": volume,
                "entry_price": entry_price,
                "tp": tp_val,
                "sl": sl_val,
                "status": "Running",
                "pnl": None,
                "close_price": None,
                "provider": row.get("Provider", "System"),
                "open_time": row.get("Timestamp", ""),
                "close_time": None,
                "events": [{
                    "time": row.get("Timestamp", ""),
                    "type": event,
                    "detail": f"{'Adopted' if event == 'ADOPT' else 'Opened'} {signal} {row.get('Symbol', '')} @ {entry_price}",
                    "notes": notes
                }]
            }
        elif ticket in trades_by_ticket:
            trade = trades_by_ticket[ticket]
            evt_time = row.get("Timestamp", "")
            notes = row.get("Notes", "")
            
            if event.startswith("CLOSE"):
                try:
                    pnl = float(row.get("Profit", 0))
                except (ValueError, TypeError):
                    pnl = 0.0
                try:
                    close_price = float(row.get("Price", 0))
                except (ValueError, TypeError):
                    close_price = 0.0
                    
                trade["status"] = "Closed"
                trade["pnl"] = pnl
                trade["close_price"] = close_price
                trade["close_time"] = evt_time
                trade["events"].append({
                    "time": evt_time,
                    "type": "CLOSE",
                    "detail": f"Closed @ {close_price} | PnL: ${pnl:.2f}",
                    "notes": notes
                })
            elif event == "UPDATE_SL":
                try:
                    new_sl = float(row.get("Price", 0))
                except (ValueError, TypeError):
                    new_sl = 0.0
                trade["sl"] = new_sl
                trade["events"].append({
                    "time": evt_time,
                    "type": "SL_UPDATE",
                    "detail": f"SL moved to {new_sl}",
                    "notes": notes
                })
            elif event == "UPDATE_TP":
                try:
                    new_tp = float(row.get("Price", 0))
                except (ValueError, TypeError):
                    new_tp = 0.0
                trade["tp"] = new_tp if new_tp != 0.0 else None
                trade["events"].append({
                    "time": evt_time,
                    "type": "TP_UPDATE",
                    "detail": f"TP moved to {new_tp}" if new_tp != 0.0 else "TP removed (open)",
                    "notes": notes
                })
    
    # Convert to list, assign sr_no, newest first
    trades = list(trades_by_ticket.values())
    trades.sort(key=lambda t: t["open_time"], reverse=True)
    for i, t in enumerate(trades):
        t["sr_no"] = i + 1
    
    return trades

@app.get("/api/events")
def get_events():
    """Return raw chronological events from trades_journal.csv."""
    import csv
    import os
    try:
        from src.journal import sync_journal_with_mt5
        sync_journal_with_mt5()
    except Exception:
        pass

    from src.journal import get_journal_file
    journal_path = os.path.join(os.getcwd(), get_journal_file())
    if not os.path.exists(journal_path):
        return []
    
    events = []
    try:
        with open(journal_path, mode='r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if "MagicMock" in str(row.get("Symbol", "")) or "MagicMock" in str(row.get("Profit", "")):
                    continue
                events.append(row)
        events.reverse()
    except Exception as e:
        logging.error(f"Failed to read events: {e}")
    return events

@app.post("/api/history/clear")
def clear_history():
    """Clear all records from trades_journal.csv, preserving only the header row."""
    import csv
    import os
    from src.journal import get_journal_file
    journal_path = os.path.join(os.getcwd(), get_journal_file())
    header = ['Timestamp', 'Provider', 'EventType', 'Symbol', 'Action', 'Price', 'Volume', 'Ticket', 'Profit', 'Notes']
    try:
        with open(journal_path, mode='w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(header)
        return {"success": True, "message": "Trading journal cleared"}
    except Exception as e:
        logging.error(f"Failed to clear history: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to clear history: {str(e)}")

@app.get("/api/config")
def get_config():
    return service.get_config()

@app.post("/api/config")
def update_config(payload: ConfigPayload):
    success = service.save_config(payload.model_dump(exclude_unset=True))
    return {"success": success, "config": service.get_config()}

@app.post("/api/test/telegram")
def test_telegram(payload: Optional[TelegramTestPayload] = None):
    cfg = service.get_config()
    token = (payload.token if payload and payload.token else cfg.get("TG_TOKEN", "")).strip()
    chat_id = (payload.chat_id if payload and payload.chat_id else cfg.get("TG_CHAT_ID", "")).strip()
    
    if not token or not chat_id:
        raise HTTPException(status_code=400, detail="Telegram Token or Chat ID missing")
        
    ok = send_telegram_message(token, chat_id, "🤖 Discord-MT5 AutoTrader: Test notification from Web Dashboard!")
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to send Telegram message. Check token and chat ID.")
    return {"success": True, "message": "Test message delivered to Telegram!"}

@app.post("/api/test/telegram-error")
def test_telegram_error(payload: Optional[TelegramTestPayload] = None):
    cfg = service.get_config()
    token = (payload.token if payload and payload.token else cfg.get("TG_TOKEN", "")).strip()
    chat_id = (payload.chat_id if payload and payload.chat_id else cfg.get("TG_CHAT_ID", "")).strip()
    
    if not token or not chat_id:
        raise HTTPException(status_code=400, detail="Telegram Token or Chat ID missing")
        
    ok = send_telegram_error(
        "🚨 AutoTrader Test Error Alert: Verifying Telegram error delivery pipeline from Web Dashboard!",
        context="Manual Web UI Test",
        token=token,
        chat_id=chat_id,
        force=True
    )
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to send Telegram error message. Check token and chat ID.")
    return {"success": True, "message": "Test error alert delivered to Telegram!"}

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    err_text = f"Unhandled server error on {request.method} {request.url.path}: {exc}"
    logging.error(err_text, exc_info=True)
    send_telegram_error(err_text, context=f"Web API ({request.method} {request.url.path})")
    return JSONResponse(status_code=500, content={"success": False, "detail": str(exc)})


@app.get("/api/terminals/status")
def get_terminals_status():
    return service.get_terminals_status()

@app.post("/api/terminals/launch")
def launch_terminals(payload: Optional[LaunchTerminalPayload] = None):
    which = payload.terminal if payload and payload.terminal else "all"
    res = service.launch_terminals(which)
    return res

@app.post("/api/test/simulate-signal")
def simulate_signal(payload: SimulateSignalPayload):
    if not payload.text.strip():
        raise HTTPException(status_code=400, detail="Signal text cannot be empty")
    return service.simulate_signal(
        payload.text,
        execute=payload.execute,
        lot_size=payload.lot_size or 0.01,
        channel=payload.channel or "channel-a"
    )

@app.post("/api/test/mock-trigger")
def mock_trigger(payload: MockTriggerPayload):
    res = service.mock_trigger_channel(payload.channel, payload.text)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("message", "Mock trigger failed"))
    return res

@app.post("/api/trades/reconcile")
def reconcile_trades():
    """Scans MT5 terminals and active Discord channels to adopt open trades and catch up on SL/TP/Breakeven signals."""
    return service.reconcile_and_adopt_trades()

@app.post("/api/trades/adopt")
def adopt_trade(payload: AdoptTradePayload):
    """Manually adopt an open MT5 trade and assign it to a specific Discord signal channel."""
    res = service.manually_adopt_trade(payload.ticket, payload.channel, payload.symbol)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("message", "Adoption failed"))
    return res

@app.post("/api/trades/unadopt/{ticket}")
def unadopt_trade(ticket: int):
    """Unadopt a trade, returning it to unmanaged status."""
    res = service.manually_unadopt_trade(ticket)
    if not res.get("success"):
        raise HTTPException(status_code=400, detail=res.get("message", "Unadopt failed"))
    return res

# Serve index.html
@app.get("/", response_class=HTMLResponse)
def serve_index():
    template_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    if os.path.exists(template_path):
        with open(template_path, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>AutoTrader Web UI Template Not Found</h1>"
