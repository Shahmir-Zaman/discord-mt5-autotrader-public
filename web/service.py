import os
import sys
import time
import logging
import threading
import collections
import requests
from typing import Dict, Any, List, Optional
from dotenv import load_dotenv

import MetaTrader5 as mt5
from src.parser import parse_signal, SignalType
from src.executor import MT5Executor, MT5_LOCK
from src.notifier import send_telegram_message, send_telegram_error, setup_telegram_error_logging
from src.extractor import extract_discord_message, extract_recent_discord_messages
from src.journal import log_trade_event

class WebLogHandler(logging.Handler):
    def __init__(self, log_buffer):
        super().__init__()
        self.log_buffer = log_buffer

    def emit(self, record):
        try:
            log_entry = {
                "timestamp": time.strftime("%H:%M:%S"),
                "level": record.levelname,
                "message": self.format(record)
            }
            self.log_buffer.append(log_entry)
        except Exception:
            self.handleError(record)

_running_paths_cache = {"time": 0.0, "paths": []}

def get_running_terminal_paths() -> List[str]:
    """Returns normalized lowercase paths of all running terminal64.exe processes in ~30ms."""
    import time
    now = time.time()
    if now - _running_paths_cache["time"] < 2.0 and _running_paths_cache["paths"]:
        return list(_running_paths_cache["paths"])

    paths = []
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        psapi = ctypes.WinDLL('psapi', use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

        pids = (wintypes.DWORD * 2048)()
        cb_needed = wintypes.DWORD()
        if psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(cb_needed)):
            count = cb_needed.value // ctypes.sizeof(wintypes.DWORD)
            buf = ctypes.create_unicode_buffer(1024)
            for i in range(count):
                pid = pids[i]
                if pid == 0:
                    continue
                h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                if h:
                    size = wintypes.DWORD(1024)
                    if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                        val = buf.value
                        if "terminal64" in val.lower():
                            paths.append(os.path.normpath(val).lower())
                    kernel32.CloseHandle(h)
    except Exception:
        pass

    if not paths and os.name == 'nt':
        try:
            import subprocess
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", "Get-Process -Name 'terminal64' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Path"],
                text=True,
                timeout=2
            )
            paths = [os.path.normpath(p.strip()).lower() for p in out.strip().splitlines() if p.strip()]
        except Exception:
            pass

    _running_paths_cache["time"] = now
    _running_paths_cache["paths"] = paths
    return paths

def launch_terminal_by_path(path: str) -> Dict[str, Any]:
    """Launches terminal64.exe at specified path if not already running."""
    import subprocess
    if not path or not os.path.exists(path):
        return {"success": False, "error": f"Path not found: {path}"}
        
    running_paths = get_running_terminal_paths()
    norm_path = os.path.normpath(path).lower()
    if any(norm_path == rp for rp in running_paths):
        return {"success": True, "already_running": True, "message": "Terminal is already running"}
        
    try:
        DETACHED_PROCESS = 0x00000008
        subprocess.Popen([path], creationflags=DETACHED_PROCESS, close_fds=True)
        return {"success": True, "already_running": False, "message": f"Terminal launched: {os.path.basename(path)}"}
    except Exception as e:
        return {"success": False, "error": str(e)}

class BotService:
    _instance = None
    _lock = threading.Lock()

    def __init__(self):
        self.is_running = False
        self.trigger_mode = "polling"  # "polling" (auto-detect in browser) or "ntfy" (push alerts)
        self.active_trader = os.getenv("ACTIVE_TRADER", "all").lower().strip()  # "provider_a", "provider_b", or "all"
        self.port = os.environ.get("AUTOTRADER_PORT", "8000")
        self.instance_id = os.environ.get("AUTOTRADER_INSTANCE", f"instance_{self.port}")
        self.target_terminal = os.getenv("TARGET_TERMINAL", "auto").strip()
        self.bot_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.logs = collections.deque(maxlen=200)
        self.processed_ids = collections.OrderedDict()
        self.executor = None
        self.extension_status = {
            "connected": False,
            "last_seen": 0.0,
            "channel": "",
            "channel_id": "",
            "last_message": ""
        }
        
        # Setup custom logging handler to stream logs to web UI
        self.log_handler = WebLogHandler(self.logs)
        self.log_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s", datefmt="%H:%M:%S"))
        logging.getLogger().addHandler(self.log_handler)
        setup_telegram_error_logging()
        self._start_heartbeat_worker()

    def _start_heartbeat_worker(self):
        if hasattr(self, "_heartbeat_thread") and self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return
        self._heartbeat_running = True
        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()
        
        import atexit
        atexit.register(self._on_shutdown)
        logging.info("AutoTrader Heartbeat & Watchdog worker launched.")

    def _on_shutdown(self):
        """Called on process shutdown to ping /fail and send Telegram alert."""
        try:
            cfg = self.get_config()
            ping_url = (cfg.get("HEARTBEAT_PING_URL") or os.getenv("HEARTBEAT_PING_URL", "")).strip()
            tg_token = cfg.get("TG_TOKEN") or os.getenv("TG_TOKEN")
            tg_chat = cfg.get("TG_CHAT_ID") or os.getenv("TG_CHAT_ID")
            
            if ping_url:
                import requests
                fail_url = ping_url.rstrip("/") + "/fail"
                requests.post(fail_url, data=f"AutoTrader Port {self.port} process stopped.", timeout=5)
                
            if tg_token and tg_chat:
                from src.notifier import send_telegram_message
                send_telegram_message(tg_token, tg_chat, f"🚨 [AutoTrader Alert]\nBot instance on port {self.port} has STOPPED or exited.")
        except Exception:
            pass

    def _heartbeat_loop(self):
        import time
        import requests
        last_extension_alert_time = 0.0
        extension_was_offline = False

        while getattr(self, "_heartbeat_running", True):
            try:
                cfg = self.get_config()
                ping_url = (cfg.get("HEARTBEAT_PING_URL") or os.getenv("HEARTBEAT_PING_URL", "")).strip()
                tg_token = cfg.get("TG_TOKEN") or os.getenv("TG_TOKEN")
                tg_chat = cfg.get("TG_CHAT_ID") or os.getenv("TG_CHAT_ID")

                # 1. Ping Healthchecks.io if configured
                if ping_url:
                    try:
                        ext_stat = self.get_extension_status()
                        term_stat = self.get_terminals_status()
                        status_summary = (
                            f"Port {self.port} | "
                            f"Extension: {'Connected' if ext_stat.get('connected') else 'Disconnected'} | "
                            f"Terminals: {'All Running' if term_stat.get('all_running') else 'Partial'}"
                        )
                        requests.post(ping_url, data=status_summary, timeout=8)
                    except Exception as pe:
                        logging.debug(f"Heartbeat ping to {ping_url} failed: {pe}")

                # 2. Check Discord Extension status
                ext_stat = self.get_extension_status()
                seconds_ago = ext_stat.get("seconds_ago", 999)
                is_ext_down = (seconds_ago > 60)

                if is_ext_down:
                    now = time.time()
                    if (not extension_was_offline or (now - last_extension_alert_time) > 300) and tg_token and tg_chat:
                        from src.notifier import send_telegram_message
                        last_extension_alert_time = now
                        extension_was_offline = True
                        send_telegram_message(
                            tg_token, tg_chat,
                            f"⚠️ [AutoTrader Alert]\nDiscord Chrome Extension has not sent a heartbeat for {int(seconds_ago)}s!\n"
                            f"Please ensure Discord is open in Chrome and the AutoTrader extension is enabled."
                        )
                else:
                    if extension_was_offline and tg_token and tg_chat:
                        from src.notifier import send_telegram_message
                        extension_was_offline = False
                        send_telegram_message(
                            tg_token, tg_chat,
                            f"✅ [AutoTrader Alert]\nDiscord Chrome Extension has RECONNECTED! (Channel: #{ext_stat.get('channel')})"
                        )

            except Exception as loop_err:
                logging.debug(f"Heartbeat loop exception: {loop_err}")

            time.sleep(45.0)

    @classmethod
    def get_instance(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = BotService()
            return cls._instance

    def set_active_trader(self, trader: str) -> Dict[str, Any]:
        """Switch target trader between 'provider_a', 'provider_b', 'provider_c', 'provider_d', and 'all'."""
        cleaned = (trader or "all").lower().strip()
        if cleaned not in ["provider_a", "provider_b", "provider_c", "provider_d", "all"]:
            return {"success": False, "message": "Invalid trader target. Use 'provider_a', 'provider_b', 'provider_c', 'provider_d', or 'all'."}
        
        self.active_trader = cleaned
        os.environ["ACTIVE_TRADER"] = cleaned
        self.save_config({"ACTIVE_TRADER": cleaned})
        logging.info(f"Target trader switched to: {cleaned.upper()}")
        return {"success": True, "active_trader": self.active_trader}

    def set_target_terminal(self, target: str) -> Dict[str, Any]:
        """Switch target MT5 terminal between 'terminal_1', 'terminal_2', 'auto', or custom path."""
        cleaned = (target or "auto").strip()
        self.target_terminal = cleaned
        os.environ["TARGET_TERMINAL"] = cleaned
        self.save_config({"TARGET_TERMINAL": cleaned})
        logging.info(f"Target MT5 terminal set to: {cleaned}")
        return {"success": True, "target_terminal": self.target_terminal}

    def get_target_terminal(self) -> str:
        """Returns the currently configured target MT5 terminal."""
        cfg = self.get_config()
        return cfg.get("TARGET_TERMINAL", getattr(self, "target_terminal", "auto"))

    def get_instance_info(self) -> Dict[str, Any]:
        """Returns metadata about the active bot instance and terminal targeting."""
        cfg = self.get_config()
        target_term = cfg.get("TARGET_TERMINAL", getattr(self, "target_terminal", "auto"))
        return {
            "port": int(self.port) if str(self.port).isdigit() else 8000,
            "instance_id": getattr(self, "instance_id", f"instance_{self.port}"),
            "target_terminal": target_term,
            "active_trader": getattr(self, "active_trader", "all"),
            "trigger_mode": getattr(self, "trigger_mode", "polling"),
            "bot_running": getattr(self, "is_running", False),
            "terminals": self.get_terminals_status()
        }

    def set_trigger_mode(self, mode: str) -> Dict[str, Any]:
        """Switch trigger mode between 'polling' and 'ntfy'."""
        if mode not in ["polling", "ntfy"]:
            return {"success": False, "message": "Invalid mode. Use 'polling' or 'ntfy'."}
        
        self.trigger_mode = mode
        logging.info(f"Trigger mode switched to: {mode.upper()}")
        
        # If currently running, restart bot in the new mode
        if self.is_running:
            self.stop_bot(wait=True)
            self.start_bot()
            
        return {"success": True, "trigger_mode": self.trigger_mode}

    def get_executor(self) -> MT5Executor:
        if self.executor is None:
            self.executor = MT5Executor()
        return self.executor

    def get_active_breakeven_triggers(self) -> List[Dict[str, Any]]:
        exec_obj = self.get_executor()
        return exec_obj.get_active_breakeven_triggers()

    def launch_terminals(self, which: str = "all") -> Dict[str, Any]:
        """Launches one or both terminals."""
        cfg = self.get_config()
        provider_a_path = cfg.get("PROVIDER_A_MT5_PATH", r"C:\Program Files\MetaTrader 5\terminal64.exe")
        provider_b_path = cfg.get("PROVIDER_B_MT5_PATH", r"C:\Program Files\MetaTrader 5 - provider_b\terminal64.exe")
        terminal_owner_path = cfg.get("TERMINAL_OWNER_MT5_PATH", r"C:\Program Files\MetaTrader 5 - terminal_owner\terminal64.exe")
        
        results = {}
        if which in ["provider_a", "all"]:
            results["provider_a"] = launch_terminal_by_path(provider_a_path)
        if which in ["provider_b", "all"]:
            results["provider_b"] = launch_terminal_by_path(provider_b_path)
        if which in ["terminal_owner", "all"]:
            results["terminal_owner"] = launch_terminal_by_path(terminal_owner_path)
            
        launched = []
        already_running = []
        errors = []
        for k, res in results.items():
            if isinstance(res, dict):
                if res.get("already_running"):
                    already_running.append(k)
                elif res.get("success"):
                    launched.append(k)
                elif res.get("error"):
                    errors.append(f"{k}: {res['error']}")
            elif res:
                launched.append(k)
                
        success = (len(launched) > 0 or len(already_running) > 0)
        return {
            "success": success,
            "results": results,
            "launched": launched,
            "already_running": already_running,
            "errors": errors
        }

    def get_terminals_status(self) -> Dict[str, Any]:
        """Returns running status and account info for both provider_a and provider_b terminals."""
        import time
        now = time.time()
        if hasattr(self, "_terminals_status_cache") and (now - self._terminals_status_cache["time"] < 1.5):
            return self._terminals_status_cache["data"]

        cfg = self.get_config()
        provider_a_path = cfg.get("PROVIDER_A_MT5_PATH", r"C:\Program Files\MetaTrader 5\terminal64.exe")
        provider_b_path = cfg.get("PROVIDER_B_MT5_PATH", r"C:\Program Files\MetaTrader 5 - provider_b\terminal64.exe")
        terminal_owner_path = cfg.get("TERMINAL_OWNER_MT5_PATH", r"C:\Program Files\MetaTrader 5 - terminal_owner\terminal64.exe")
        
        running_paths = get_running_terminal_paths()
        
        def _inspect(path: str, label: str):
            norm = os.path.normpath(path).lower() if path else ""
            is_run = any(norm == rp for rp in running_paths) if norm else False
            info = {
                "name": label,
                "path": path,
                "running": is_run,
                "connected": False,
                "login": None,
                "server": None,
                "balance": 0.0,
                "equity": 0.0,
                "profit": 0.0,
                "margin_free": 0.0,
                "currency": "USD"
            }
            if is_run and os.path.exists(path):
                try:
                    with MT5_LOCK:
                        if mt5.initialize(path=path):
                            acc = mt5.account_info()
                            if acc:
                                info["connected"] = True
                                info["login"] = acc.login
                                info["server"] = acc.server
                                info["balance"] = round(acc.balance, 2)
                                info["equity"] = round(acc.equity, 2)
                                info["profit"] = round(acc.profit, 2)
                                info["margin_free"] = round(acc.margin_free, 2)
                                info["currency"] = acc.currency
                except Exception as e:
                    logging.warning(f"Error inspecting terminal {label}: {e}")
            return info

        provider_a_data = _inspect(provider_a_path, "provider_a (#channel-a)")
        provider_b_data = _inspect(provider_b_path, "provider_b (#channel-b)")
        terminal_owner_data = _inspect(terminal_owner_path, "terminal_owner (Terminal 3)")
        
        result = {
            "provider_a": provider_a_data,
            "provider_b": provider_b_data,
            "terminal_owner": terminal_owner_data,
            "both_running": (provider_a_data["running"] and provider_b_data["running"]),
            "any_running": (provider_a_data["running"] or provider_b_data["running"] or terminal_owner_data["running"]),
            "all_running": (provider_a_data["running"] and provider_b_data["running"] and terminal_owner_data["running"])
        }
        self._terminals_status_cache = {"time": now, "data": result}
        return result

    def get_mt5_account_info(self) -> Dict[str, Any]:
        """Fetch live MT5 account details across both terminals."""
        t_status = self.get_terminals_status()
        provider_a = t_status.get("provider_a", {})
        provider_b = t_status.get("provider_b", {})
        terminal_owner = t_status.get("terminal_owner", {})
        
        target_term = self.get_target_terminal().lower()
        if target_term in ["terminal_3", "terminal_owner"]:
            primary = terminal_owner if terminal_owner.get("connected") else (provider_a if provider_a.get("connected") else provider_b)
        elif target_term in ["terminal_2", "provider_b"]:
            primary = provider_b if provider_b.get("connected") else (provider_a if provider_a.get("connected") else terminal_owner)
        elif getattr(self, "active_trader", "all") in ["provider_b", "provider_c"]:
            primary = provider_b if provider_b.get("connected") else provider_a
        else:
            primary = provider_a if provider_a.get("connected") else (provider_b if provider_b.get("connected") else terminal_owner)
        
        return {
            "connected": t_status["any_running"] and (provider_a["connected"] or provider_b["connected"]),
            "active_trader": getattr(self, "active_trader", "all"),
            "login": primary.get("login"),
            "server": primary.get("server"),
            "balance": primary.get("balance", 0.0),
            "equity": primary.get("equity", 0.0),
            "profit": primary.get("profit", 0.0),
            "margin_free": primary.get("margin_free", 0.0),
            "currency": primary.get("currency", "USD"),
            "terminals": t_status
        }

    def get_open_positions(self) -> List[Dict[str, Any]]:
        """Fetch open positions and pending orders from both MT5 terminals."""
        cfg = self.get_config()
        terminals = [
            ("provider_a", cfg.get("PROVIDER_A_MT5_PATH", r"C:\Program Files\MetaTrader 5\terminal64.exe")),
            ("provider_b", cfg.get("PROVIDER_B_MT5_PATH", r"C:\Program Files\MetaTrader 5 - provider_b\terminal64.exe")),
            ("terminal_owner", cfg.get("TERMINAL_OWNER_MT5_PATH", r"C:\Program Files\MetaTrader 5 - terminal_owner\terminal64.exe"))
        ]
        
        order_type_map = {
            2: "BUY LIMIT",
            3: "SELL LIMIT",
            4: "BUY STOP",
            5: "SELL STOP",
            6: "BUY STOP LIMIT",
            7: "SELL STOP LIMIT"
        }
        
        all_positions = []
        exec_obj = self.get_executor()
        with MT5_LOCK:
            for provider_name, path in terminals:
                if not path or not os.path.exists(path):
                    continue
                try:
                    if not mt5.initialize(path=path):
                        continue
                except Exception:
                    continue
                    
                acc = mt5.account_info()
                login_id = acc.login if acc else "Unknown"
                
                positions = mt5.positions_get()
                if positions:
                    for pos in positions:
                        is_adopted = (pos.ticket in exec_obj.adopted_tickets)
                        adopted_meta = exec_obj.adopted_metadata.get(pos.ticket, {})
                        all_positions.append({
                            "ticket": pos.ticket,
                            "symbol": pos.symbol,
                            "type": "BUY" if pos.type == 0 else "SELL",
                            "volume": pos.volume,
                            "price_open": pos.price_open,
                            "price_current": pos.price_current,
                            "sl": pos.sl,
                            "tp": pos.tp,
                            "profit": round(pos.profit, 2),
                            "magic": pos.magic,
                            "is_pending": False,
                            "is_bot_trade": (pos.magic == 123456 or is_adopted),
                            "is_adopted": is_adopted,
                            "adopted_channel": adopted_meta.get("channel", ""),
                            "provider": provider_name,
                            "account": f"{provider_name} ({login_id})"
                        })
                        
                orders = mt5.orders_get()
                if orders:
                    for order in orders:
                        typ = order_type_map.get(order.type, "PENDING")
                        is_adopted = (order.ticket in exec_obj.adopted_tickets)
                        adopted_meta = exec_obj.adopted_metadata.get(order.ticket, {})
                        all_positions.append({
                            "ticket": order.ticket,
                            "symbol": order.symbol,
                            "type": typ,
                            "volume": order.volume_initial,
                            "price_open": order.price_open,
                            "price_current": order.price_current,
                            "sl": order.sl,
                            "tp": order.tp,
                            "profit": 0.0,
                            "magic": order.magic,
                            "is_pending": True,
                            "is_bot_trade": (order.magic == 123456 or is_adopted),
                            "is_adopted": is_adopted,
                            "adopted_channel": adopted_meta.get("channel", ""),
                            "provider": provider_name,
                            "account": f"{provider_name} ({login_id})"
                        })
                        
        return all_positions

    def close_position_by_ticket(self, ticket: int) -> Dict[str, Any]:
        """Close a specific position or cancel a pending order across terminals."""
        cfg = self.get_config()
        terminals = [
            ("provider_a", cfg.get("PROVIDER_A_MT5_PATH", r"C:\Program Files\MetaTrader 5\terminal64.exe")),
            ("provider_b", cfg.get("PROVIDER_B_MT5_PATH", r"C:\Program Files\MetaTrader 5 - provider_b\terminal64.exe")),
            ("terminal_owner", cfg.get("TERMINAL_OWNER_MT5_PATH", r"C:\Program Files\MetaTrader 5 - terminal_owner\terminal64.exe"))
        ]
        
        with MT5_LOCK:
            for provider_name, path in terminals:
                if not path or not os.path.exists(path):
                    continue
                try:
                    if not mt5.initialize(path=path):
                        continue
                except Exception:
                    continue
                    
                positions = mt5.positions_get(ticket=ticket)
                if positions:
                    pos = positions[0]
                    tick = mt5.symbol_info_tick(pos.symbol)
                    if not tick:
                        return {"success": False, "error": f"Could not get tick data for {pos.symbol}"}
                        
                    order_type = mt5.ORDER_TYPE_SELL if pos.type == 0 else mt5.ORDER_TYPE_BUY
                    price = tick.bid if pos.type == 0 else tick.ask
                    
                    exec_obj = self.get_executor()
                    request = {
                        "action": mt5.TRADE_ACTION_DEAL,
                        "symbol": pos.symbol,
                        "position": pos.ticket,
                        "volume": float(pos.volume),
                        "type": order_type,
                        "price": price,
                        "deviation": 20,
                        "magic": pos.magic,
                        "comment": "Web UI Close",
                        "type_time": mt5.ORDER_TIME_GTC,
                        "type_filling": exec_obj._get_filling_mode(pos.symbol),
                    }
                    
                    res = mt5.order_send(request)
                    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                        from src.journal import log_trade_event
                        log_trade_event("CLOSE_MANUAL", pos.symbol, "CLOSE", price, pos.volume, pos.ticket, pos.profit, "Closed via Web UI", provider=provider_name)
                        return {"success": True, "message": f"Closed #{ticket} on {provider_name} at {price}"}
                        
                    err = mt5.last_error()
                    comment = res.comment if res else "Unknown"
                    retcode = res.retcode if res else err
                    return {"success": False, "error": f"Failed on {provider_name}: {comment} (Code: {retcode})"}
                    
                orders = mt5.orders_get(ticket=ticket)
                if orders:
                    order = orders[0]
                    request = {
                        "action": mt5.TRADE_ACTION_REMOVE,
                        "order": order.ticket
                    }
                    res = mt5.order_send(request)
                    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                        from src.journal import log_trade_event
                        log_trade_event("CLOSE_MANUAL", order.symbol, "CANCEL", order.price_open, order.volume_initial, order.ticket, 0.0, "Cancelled via Web UI", provider=provider_name)
                        return {"success": True, "message": f"Cancelled #{ticket} on {provider_name}"}
                        
                    err = mt5.last_error()
                    comment = res.comment if res else "Unknown"
                    retcode = res.retcode if res else err
                    return {"success": False, "error": f"Cancel failed on {provider_name}: {comment} (Code: {retcode})"}
                    
            return {"success": False, "error": f"Ticket #{ticket} not found on any MT5 terminal"}

    def check_cdp_status(self, port: int = 9222) -> Dict[str, Any]:
        """Check if Chrome/Brave remote debugging is accessible."""
        import time
        import socket
        now = time.time()
        if hasattr(self, "_cdp_status_cache") and (now - self._cdp_status_cache["time"] < 3.0):
            return self._cdp_status_cache["data"]

        # Quick TCP probe: if port is closed, fails in 1ms instead of waiting 2s
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.15)
                if s.connect_ex(('127.0.0.1', port)) != 0:
                    res = {"connected": False, "total_tabs": 0, "discord_tabs": []}
                    self._cdp_status_cache = {"time": now, "data": res}
                    return res
        except Exception:
            pass

        try:
            r = requests.get(f"http://127.0.0.1:{port}/json", timeout=0.5)
            if r.status_code == 200:
                tabs = r.json()
                discord_tabs = [t.get("title", "") for t in tabs if "discord.com" in t.get("url", "")]
                res = {
                    "connected": True,
                    "total_tabs": len(tabs),
                    "discord_tabs": discord_tabs
                }
                self._cdp_status_cache = {"time": now, "data": res}
                return res
        except Exception:
            pass
        res = {"connected": False, "total_tabs": 0, "discord_tabs": []}
        self._cdp_status_cache = {"time": now, "data": res}
        return res

    def _is_message_valid(self, msg_id: str, ntfy_time: Optional[float] = None, max_age: float = 300.0) -> bool:
        """
        Validates message age using Discord's Snowflake ID timestamp.
        """
        import time
        if not msg_id or not msg_id.startswith("chat-messages-"):
            return True
            
        try:
            parts = msg_id.split("-")
            snowflake_str = parts[-1]
            snowflake = int(snowflake_str)
            msg_time = ((snowflake >> 22) + 1420070400000) / 1000.0
            current_time = time.time()
            
            # Max Age Check: Ignore if message is older than max_age seconds
            age = current_time - msg_time
            if age > max_age:
                logging.warning(f"Ignored message {msg_id}: Too old ({age/60:.1f} mins ago, limit {max_age/60:.1f} mins).")
                return False
                    
            return True
        except Exception as e:
            logging.error(f"Failed to validate message timestamp: {e}")
            return True

    def simulate_signal(self, text: str, execute: bool = False, lot_size: float = 0.01, channel: str = "provider_a") -> Dict[str, Any]:
        """Parse a signal and optionally execute it in MT5."""
        signal = parse_signal(text)
        result = {
            "raw_text": text,
            "parsed": {
                "type": signal["type"].value,
                "action": signal.get("action"),
                "symbol": signal.get("symbol"),
                "price": signal.get("price"),
                "price2": signal.get("price2"),
                "trigger_price": signal.get("trigger_price"),
                "sl": signal.get("sl"),
                "tp": signal.get("tp")
            },
            "executed": False,
            "execution_result": None,
            "channel": channel
        }
        
        if execute and signal["type"] != SignalType.UNKNOWN:
            exec_obj = self.get_executor()
            result_msg = exec_obj.execute_signal(signal, channel=channel, config=self.get_config())
            result["executed"] = True
            result["execution_result"] = result_msg
            
        return result

    def mock_trigger_channel(self, channel: str, text: Optional[str] = None) -> Dict[str, Any]:
        """Trigger an instant signal simulation or live scrape for a channel."""
        load_dotenv(override=True)
        tg_token = os.getenv("TG_TOKEN", "")
        tg_chat_id = os.getenv("TG_CHAT_ID", "")
        lot_size = float(os.getenv("LOT_SIZE", "0.01"))

        if not text:
            # Scrape live from browser tab
            msg_id, text, detected = extract_discord_message(channel_keyword=channel)
            if not text:
                return {"success": False, "message": f"No message extracted from #{channel} tab in browser."}

        signal = parse_signal(text)
        if signal["type"] == SignalType.UNKNOWN:
            return {"success": False, "message": f"Signal in #{channel} parsed as UNKNOWN: '{text}'"}

        exec_obj = self.get_executor()
        result_msg = exec_obj.execute_signal(signal, channel=channel, config=self.get_config())
        
        if tg_token and tg_chat_id:
            formatted_msg = f"🔔 [#{channel} - TEST TRIGGER]\n{result_msg}"
            send_telegram_message(tg_token, tg_chat_id, formatted_msg)
            
        logging.info(f"[#{channel} TEST TRIGGER] {result_msg}")
        return {
            "success": True,
            "channel": channel,
            "text": text,
            "parsed": {
                "type": signal["type"].value,
                "symbol": signal.get("symbol"),
                "action": signal.get("action"),
                "price": signal.get("price")
            },
            "execution_result": result_msg
        }

    def record_extension_heartbeat(self, channel: str = "", channel_id: str = ""):
        """Record heartbeat from Discord Chrome extension."""
        self.extension_status["connected"] = True
        self.extension_status["last_seen"] = time.time()
        if channel:
            self.extension_status["channel"] = channel
        if channel_id:
            self.extension_status["channel_id"] = channel_id

    def get_extension_status(self) -> Dict[str, Any]:
        """Return the live connection status of the Discord Chrome extension."""
        now = time.time()
        last_seen = self.extension_status.get("last_seen", 0.0)
        connected = (now - last_seen) < 30.0 if last_seen > 0 else False
        return {
            "connected": connected,
            "last_seen": last_seen,
            "seconds_ago": int(now - last_seen) if last_seen > 0 else None,
            "channel": self.extension_status.get("channel", ""),
            "channel_id": self.extension_status.get("channel_id", ""),
            "last_message": self.extension_status.get("last_message", "")
        }

    def _resolve_extension_channel(self, channel: str, channel_id: str = "", author: str = "") -> Optional[str]:
        """Resolves raw channel title, channel ID, or author into canonical channel keyword."""
        from src.extractor import CHANNEL_ALIASES
        from main import CHANNEL_ROUTING

        chan_clean = (channel or "").lower().replace("#", "").strip()
        auth_clean = (author or "").lower().strip()
        cid_clean = (channel_id or "").strip()

        # 1. Match against known channel aliases (e.g. snowflake ID, keyword aliases)
        for canon, aliases in CHANNEL_ALIASES.items():
            for a in aliases:
                if (cid_clean and a == cid_clean) or (chan_clean and a in chan_clean) or (auth_clean and a in auth_clean):
                    return canon

        # 2. Match against CHANNEL_ROUTING
        for target, keywords in CHANNEL_ROUTING.items():
            for kw in keywords:
                if (chan_clean and kw in chan_clean) or (auth_clean and kw in auth_clean):
                    return target

        # 3. Direct canonical match fallback
        if chan_clean in ["channel-a", "channel-b", "channel-c", "provider_d"]:
            return chan_clean

        return None

    def handle_extension_message(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Processes an incoming message pushed directly from the Discord Chrome Extension."""
        channel = data.get("channel", "").strip()
        channel_id = data.get("channel_id", "").strip()
        msg_id = data.get("message_id", "").strip()
        text = data.get("text", "").strip()
        author = data.get("author", "").strip()

        self.record_extension_heartbeat(channel=channel, channel_id=channel_id)
        if text:
            self.extension_status["last_message"] = text[:120]

        if not text or not msg_id:
            return {"success": False, "status": "ignored", "message": "Missing message text or ID"}

        # Resolve target channel keyword
        target_channel = self._resolve_extension_channel(channel, channel_id, author)
        if not target_channel:
            logging.info(f"[Extension] Ignored message from unmapped channel '{channel}' (Author: '{author}')")
            return {"success": False, "status": "unmapped_channel", "message": f"Channel '{channel}' is not mapped to an active provider"}

        # Check active trader filter
        active = getattr(self, "active_trader", "all").lower()
        if active == "provider_a" and target_channel not in ["channel-a", "channel-a"]:
            logging.info(f"[Extension] Ignored #{target_channel} message (Bot is set to provider_a only).")
            return {"success": False, "status": "filtered", "message": "Bot is set to provider_a only"}
        if active == "provider_b" and target_channel != "channel-b":
            logging.info(f"[Extension] Ignored #{target_channel} message (Bot is set to provider_b only).")
            return {"success": False, "status": "filtered", "message": "Bot is set to provider_b only"}
        if active == "provider_c" and target_channel not in ["channel-c", "provider_c"]:
            logging.info(f"[Extension] Ignored #{target_channel} message (Bot is set to provider_c only).")
            return {"success": False, "status": "filtered", "message": "Bot is set to provider_c only"}
        if active == "provider_d" and target_channel not in ["provider_d", "🦅-provider_d", "provider_d_alias"]:
            logging.info(f"[Extension] Ignored #{target_channel} message (Bot is set to provider_d only).")
            return {"success": False, "status": "filtered", "message": "Bot is set to provider_d only"}

        dedup_key = f"{target_channel}:{msg_id}"
        if dedup_key in self.processed_ids:
            return {"success": True, "status": "duplicate", "message": "Message already processed"}

        signal = parse_signal(text)
        if signal["type"] == SignalType.UNKNOWN:
            self.processed_ids[dedup_key] = True
            if len(self.processed_ids) > 100:
                self.processed_ids.popitem(last=False)
            logging.info(f"[Extension #{target_channel}] Message parsed as UNKNOWN. Ignoring.")
            return {"success": True, "status": "unknown_signal", "message": "Signal parsed as UNKNOWN"}

        is_close_or_cancel = signal["type"] in (SignalType.CLOSE_POSITION, SignalType.CANCEL_PENDING)
        max_age = 600.0 if is_close_or_cancel else 300.0

        if not self._is_message_valid(msg_id, max_age=max_age):
            self.processed_ids[dedup_key] = True
            if len(self.processed_ids) > 100:
                self.processed_ids.popitem(last=False)
            return {"success": False, "status": "expired", "message": "Message timestamp too old"}

        self.processed_ids[dedup_key] = True
        if len(self.processed_ids) > 100:
            self.processed_ids.popitem(last=False)

        logging.info(f"[Extension #{target_channel}] New signal received: {text}")
        logging.info(f"[Extension #{target_channel}] Parsed Signal: {signal}")

        try:
            exec_obj = self.get_executor()
            result_msg = exec_obj.execute_signal(signal, channel=target_channel, config=self.get_config())
            logging.info(f"[Extension #{target_channel}] {result_msg}")

            load_dotenv(override=True)
            tg_token = os.getenv("TG_TOKEN", "")
            tg_chat_id = os.getenv("TG_CHAT_ID", "")
            
            is_error = isinstance(result_msg, str) and any(kw in result_msg.lower() for kw in [
                "error:", "trade failed", "close failed", "update failed", "rejected", "cannot", "security halt", "exception"
            ])
            
            if tg_token and tg_chat_id:
                if is_error:
                    send_telegram_error(
                        result_msg,
                        context=f"Extension Trade Issue (#{target_channel})",
                        token=tg_token,
                        chat_id=tg_chat_id,
                        force=True
                    )
                else:
                    formatted_msg = f"⚡ [Extension #{target_channel}]\n{result_msg}"
                    send_telegram_message(tg_token, tg_chat_id, formatted_msg)

            return {
                "success": not is_error,
                "status": "error" if is_error else "executed",
                "channel": target_channel,
                "signal": {
                    "type": signal["type"].value,
                    "symbol": signal.get("symbol"),
                    "action": signal.get("action"),
                    "price": signal.get("price")
                },
                "result": result_msg
            }
        except Exception as e:
            err_msg = f"Critical error processing signal in #{target_channel}: {e}"
            logging.error(err_msg, exc_info=True)
            send_telegram_error(err_msg, context=f"Extension Handler (#{target_channel})")
            return {"success": False, "status": "error", "message": err_msg}

    @staticmethod
    def _get_env_path() -> str:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        target = os.path.join(base_dir, ".env")
        return target if os.path.exists(target) else os.path.abspath(".env")

    def _get_instance_config_path(self) -> str:
        port = os.environ.get("AUTOTRADER_PORT", "8000")
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(base_dir, f"config_{port}.json")

    def get_config(self) -> Dict[str, Any]:
        """Read base config from .env, then layer instance-specific overrides."""
        env_path = self._get_env_path()
        load_dotenv(env_path, override=True)
        base_cfg = {
            "ACTIVE_TRADER": os.getenv("ACTIVE_TRADER", getattr(self, "active_trader", "all")),
            "TARGET_TERMINAL": os.getenv("TARGET_TERMINAL", getattr(self, "target_terminal", "auto")),
            "NTFY_TOPIC": os.getenv("NTFY_TOPIC", ""),
            "TG_TOKEN": os.getenv("TG_TOKEN", ""),
            "TG_CHAT_ID": os.getenv("TG_CHAT_ID", ""),
            "SYMBOL_SUFFIX": os.getenv("SYMBOL_SUFFIX", ""),
            "PROVIDER_A_SYMBOL_SUFFIX": os.getenv("PROVIDER_A_SYMBOL_SUFFIX", os.getenv("SYMBOL_SUFFIX", "")),
            "PROVIDER_B_SYMBOL_SUFFIX": os.getenv("PROVIDER_B_SYMBOL_SUFFIX", os.getenv("SYMBOL_SUFFIX", "")),
            "PROVIDER_A_LOT_MODE": os.getenv("PROVIDER_A_LOT_MODE", "FIXED"),
            "PROVIDER_A_FIXED_LOT": os.getenv("PROVIDER_A_FIXED_LOT", "0.01"),
            "PROVIDER_A_RISK_PCT": os.getenv("PROVIDER_A_RISK_PCT", "1.0"),
            "PROVIDER_B_LOT_MODE": os.getenv("PROVIDER_B_LOT_MODE", "FIXED"),
            "PROVIDER_B_FIXED_LOT": os.getenv("PROVIDER_B_FIXED_LOT", "0.01"),
            "PROVIDER_B_RISK_PCT": os.getenv("PROVIDER_B_RISK_PCT", "1.0"),
            "PROVIDER_B_DUAL_ENTRY_SPLIT": os.getenv("PROVIDER_B_DUAL_ENTRY_SPLIT", "75/25"),
            "PROVIDER_C_LOT_MODE": os.getenv("PROVIDER_C_LOT_MODE", os.getenv("PROVIDER_B_LOT_MODE", "FIXED")),
            "PROVIDER_C_FIXED_LOT": os.getenv("PROVIDER_C_FIXED_LOT", os.getenv("PROVIDER_B_FIXED_LOT", "0.01")),
            "PROVIDER_C_RISK_PCT": os.getenv("PROVIDER_C_RISK_PCT", os.getenv("PROVIDER_B_RISK_PCT", "1.0")),
            "PROVIDER_D_TARGET_TERMINAL": os.getenv("PROVIDER_D_TARGET_TERMINAL", "terminal_1"),
            "PROVIDER_D_LOT_MODE": os.getenv("PROVIDER_D_LOT_MODE", "FIXED"),
            "PROVIDER_D_FIXED_LOT": os.getenv("PROVIDER_D_FIXED_LOT", "0.01"),
            "PROVIDER_D_RISK_PCT": os.getenv("PROVIDER_D_RISK_PCT", "1.0"),
            "PROVIDER_D_SYMBOL_SUFFIX": os.getenv("PROVIDER_D_SYMBOL_SUFFIX", os.getenv("SYMBOL_SUFFIX", "")),
            "PROVIDER_D_STARTING_MILESTONE": os.getenv("PROVIDER_D_STARTING_MILESTONE", "100.0"),
            "TERMINAL_1_LOT_MODE": os.getenv("TERMINAL_1_LOT_MODE", os.getenv("PROVIDER_A_LOT_MODE", "FIXED")),
            "TERMINAL_1_FIXED_LOT": os.getenv("TERMINAL_1_FIXED_LOT", os.getenv("PROVIDER_A_FIXED_LOT", "0.12")),
            "TERMINAL_1_RISK_PCT": os.getenv("TERMINAL_1_RISK_PCT", os.getenv("PROVIDER_A_RISK_PCT", "1.0")),
            "TERMINAL_2_LOT_MODE": os.getenv("TERMINAL_2_LOT_MODE", os.getenv("PROVIDER_B_LOT_MODE", "RISK")),
            "TERMINAL_2_FIXED_LOT": os.getenv("TERMINAL_2_FIXED_LOT", os.getenv("PROVIDER_B_FIXED_LOT", "0.01")),
            "TERMINAL_2_RISK_PCT": os.getenv("TERMINAL_2_RISK_PCT", os.getenv("PROVIDER_B_RISK_PCT", "2.0")),
            "TERMINAL_3_LOT_MODE": os.getenv("TERMINAL_3_LOT_MODE", "RISK"),
            "TERMINAL_3_FIXED_LOT": os.getenv("TERMINAL_3_FIXED_LOT", "0.01"),
            "TERMINAL_3_RISK_PCT": os.getenv("TERMINAL_3_RISK_PCT", "1.0"),
            "PROVIDER_A_MT5_PATH": os.getenv("PROVIDER_A_MT5_PATH", r"C:\Program Files\MetaTrader 5\terminal64.exe"),
            "PROVIDER_B_MT5_PATH": os.getenv("PROVIDER_B_MT5_PATH", r"C:\Program Files\MetaTrader 5 - provider_b\terminal64.exe"),
            "TERMINAL_OWNER_MT5_PATH": os.getenv("TERMINAL_OWNER_MT5_PATH", r"C:\Program Files\MetaTrader 5 - terminal_owner\terminal64.exe"),
            "TERMINAL_OWNER_SYMBOL_SUFFIX": os.getenv("TERMINAL_OWNER_SYMBOL_SUFFIX", os.getenv("SYMBOL_SUFFIX", "")),
            "GOLD_PIP_THRESHOLD": os.getenv("GOLD_PIP_THRESHOLD", "50"),
            "HEARTBEAT_PING_URL": os.getenv("HEARTBEAT_PING_URL", "https://example.invalid/configure-locally")
        }

        # Overlay instance-specific overrides if file exists
        inst_path = self._get_instance_config_path()
        if os.path.exists(inst_path):
            try:
                import json
                with open(inst_path, "r", encoding="utf-8") as f:
                    overrides = json.load(f)
                    if isinstance(overrides, dict):
                        base_cfg.update(overrides)
            except Exception as e:
                logging.warning(f"Failed to read instance config from {inst_path}: {e}")
                
        return base_cfg

    def save_config(self, config_dict: Dict[str, str]) -> bool:
        """Update config values in instance config and .env."""
        import json
        allowed_keys = {
            "ACTIVE_TRADER", "TARGET_TERMINAL", "NTFY_TOPIC", "TG_TOKEN", "TG_CHAT_ID", "SYMBOL_SUFFIX",
            "PROVIDER_A_SYMBOL_SUFFIX", "PROVIDER_B_SYMBOL_SUFFIX",
            "PROVIDER_A_LOT_MODE", "PROVIDER_A_FIXED_LOT", "PROVIDER_A_RISK_PCT",
            "PROVIDER_B_LOT_MODE", "PROVIDER_B_FIXED_LOT", "PROVIDER_B_RISK_PCT",
            "PROVIDER_B_DUAL_ENTRY_SPLIT",
            "PROVIDER_C_LOT_MODE", "PROVIDER_C_FIXED_LOT", "PROVIDER_C_RISK_PCT",
            "PROVIDER_D_TARGET_TERMINAL", "PROVIDER_D_LOT_MODE", "PROVIDER_D_FIXED_LOT", "PROVIDER_D_RISK_PCT", "PROVIDER_D_SYMBOL_SUFFIX", "PROVIDER_D_STARTING_MILESTONE",
            "TERMINAL_1_LOT_MODE", "TERMINAL_1_FIXED_LOT", "TERMINAL_1_RISK_PCT",
            "TERMINAL_2_LOT_MODE", "TERMINAL_2_FIXED_LOT", "TERMINAL_2_RISK_PCT",
            "TERMINAL_3_LOT_MODE", "TERMINAL_3_FIXED_LOT", "TERMINAL_3_RISK_PCT",
            "TERMINAL_OWNER_LOT_MODE", "TERMINAL_OWNER_FIXED_LOT", "TERMINAL_OWNER_RISK_PCT",
            "PROVIDER_A_MT5_PATH", "PROVIDER_B_MT5_PATH", "TERMINAL_OWNER_MT5_PATH", "TERMINAL_OWNER_SYMBOL_SUFFIX", "TERMINAL_OWNER_MT5_LOGIN",
            "GOLD_PIP_THRESHOLD", "LOT_SIZE", "AUTO_CLOSE_PREVIOUS_ON_NEW_ENTRY",
            "HEARTBEAT_PING_URL"
        }
        
        updates = {k: str(v).strip() for k, v in config_dict.items() if k in allowed_keys}
        if "NTFY_TOPIC" in updates and updates["NTFY_TOPIC"]:
            topic = updates["NTFY_TOPIC"]
            if not topic.startswith("http://") and not topic.startswith("https://"):
                updates["NTFY_TOPIC"] = f"https://ntfy.sh/{topic}"
        for k, v in updates.items():
            os.environ[k] = v
            
        # 1. Save to instance-specific JSON file
        inst_path = self._get_instance_config_path()
        inst_data = {}
        if os.path.exists(inst_path):
            try:
                with open(inst_path, "r", encoding="utf-8") as f:
                    inst_data = json.load(f)
            except Exception:
                inst_data = {}
        inst_data.update(updates)
        try:
            with open(inst_path, "w", encoding="utf-8") as f:
                json.dump(inst_data, f, indent=2)
        except Exception as e:
            logging.warning(f"Could not write instance config to {inst_path}: {e}")
            
        # 2. Always persist to .env so settings survive server restarts and reloads
        env_path = self._get_env_path()
        existing_lines = []
        if os.path.exists(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    existing_lines = f.readlines()
            except Exception as e:
                logging.warning(f"Could not read existing .env at {env_path}: {e}")
                    
            new_lines = []
            updated_keys = set()
            
            for line in existing_lines:
                stripped = line.strip()
                if stripped.startswith("#") or not stripped or "=" not in stripped:
                    new_lines.append(line.rstrip())
                    continue
                    
                key = stripped.split("=", 1)[0].strip()
                if key in updates:
                    new_lines.append(f"{key}={updates[key]}")
                    updated_keys.add(key)
                else:
                    new_lines.append(line.rstrip())
                    
            for k, v in updates.items():
                if k not in updated_keys:
                    new_lines.append(f"{k}={v}")
                    
            try:
                with open(env_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(new_lines) + "\n")
            except Exception as e:
                logging.error(f"Failed to write .env: {e}")
                return False
                
        return True

    def reconcile_and_adopt_trades(self) -> Dict[str, Any]:
        """
        Scans MT5 terminals for active positions and pending orders.
        Fetches recent Discord messages across monitored channels via CDP.
        Matches open trades with ENTRY signals, adopts them in MT5Executor,
        and replays subsequent SL/TP/Breakeven updates to synchronize live state.
        """
        exec_obj = self.get_executor()
        cfg = self.get_config()
        tg_token = cfg.get("TG_TOKEN")
        tg_chat_id = cfg.get("TG_CHAT_ID")
        
        all_positions = self.get_open_positions()
        if not all_positions:
            return {
                "success": True,
                "adopted_count": 0,
                "adopted": [],
                "message": "No open positions or pending orders found across MT5 terminals."
            }

        # Monitored channel keywords
        channel_keywords = ["channel-a", "channel-b", "channel-c", "provider_d"]
        channel_messages: Dict[str, List[Dict[str, Any]]] = {}
        
        for ch in channel_keywords:
            try:
                msgs, detected = extract_recent_discord_messages(channel_keyword=ch, limit=25)
                if msgs:
                    channel_messages[ch] = msgs
            except Exception as e:
                logging.warning(f"Failed to fetch recent messages for #{ch} during reconciliation: {e}")

        if not channel_messages:
            return {
                "success": True,
                "adopted_count": 0,
                "adopted": [],
                "message": f"Found {len(all_positions)} open MT5 position(s), but Discord tabs were not accessible via CDP."
            }

        adopted_results = []

        for pos in all_positions:
            ticket = pos.get("ticket")
            symbol = pos.get("symbol", "")
            pos_type = str(pos.get("type", "")).upper()
            pos_price = float(pos.get("price_open", 0.0))
            is_already_adopted = ticket in exec_obj.adopted_tickets

            matched_channel = None
            matched_entry_idx = -1
            matched_entry_sig = None
            matched_entry_msg = None

            for ch, msgs in channel_messages.items():
                for idx, msg_obj in reversed(list(enumerate(msgs))):
                    text = msg_obj.get("text", "")
                    sig = parse_signal(text)
                    if sig.get("type") != SignalType.ENTRY:
                        continue
                    
                    sig_sym = sig.get("symbol", "").upper()
                    base_sig = exec_obj._get_base_symbol(sig_sym)
                    base_pos = exec_obj._get_base_symbol(symbol.upper())
                    if base_sig != base_pos:
                        continue
                    
                    sig_action = sig.get("action", "").upper()
                    if sig_action not in pos_type:
                        continue
                    
                    sig_price = sig.get("price")
                    if sig_price:
                        pip_size = exec_obj._get_pip_size(symbol)
                        diff_pips = abs(pos_price - float(sig_price)) / pip_size
                        threshold = exec_obj._get_pip_threshold(symbol) or 100
                        if diff_pips > max(threshold * 2, 100):
                            continue
                    
                    # Verify this signal was not already stopped out or closed by a later message
                    is_closed = False
                    for sub in msgs[idx + 1:]:
                        sub_txt = sub.get("text", "").lower()
                        if "stop loss hit" in sub_txt or "sl hit" in sub_txt or "closed" in sub_txt:
                            is_closed = True
                            break
                        sub_parsed = parse_signal(sub.get("text", ""))
                        if sub_parsed.get("type") == SignalType.CLOSE_POSITION:
                            is_closed = True
                            break
                    if is_closed:
                        continue
                            
                    matched_channel = ch
                    matched_entry_idx = idx
                    matched_entry_sig = sig
                    matched_entry_msg = msg_obj
                    break
                if matched_channel:
                    break

            if not matched_channel:
                logging.info(f"Open MT5 position #{ticket} ({pos_type} {symbol} @ {pos_price}) had no matching Discord ENTRY signal.")
                continue

            # 1. Adopt position into MT5Executor
            exec_obj.adopt_ticket(
                ticket,
                symbol=symbol,
                channel=matched_channel,
                metadata={
                    "entry_price": pos_price,
                    "pos_type": pos_type,
                    "signal": matched_entry_sig,
                    "message_id": matched_entry_msg.get("id", "")
                }
            )

            if not is_already_adopted:
                log_trade_event(
                    "ADOPT",
                    symbol,
                    pos_type,
                    pos_price,
                    pos.get("volume", 0.0),
                    ticket,
                    pos.get("profit", 0.0),
                    f"Adopted trade matched with #{matched_channel} signal",
                    provider=matched_channel
                )
                if tg_token and tg_chat_id:
                    formatted_msg = (
                        f"🤝 [#{matched_channel} - TRADE ADOPTED]\n"
                        f"Adopted MT5 Position #{ticket}: {pos_type} {symbol} @ {pos_price}\n"
                        f"Matched with Discord Signal: {matched_entry_sig.get('action')} @ {matched_entry_sig.get('price', pos_price)}"
                    )
                    send_telegram_message(tg_token, tg_chat_id, formatted_msg)

            entry_msg_id = matched_entry_msg.get("id", "")
            if entry_msg_id:
                self.processed_ids[f"{matched_channel}:{entry_msg_id}"] = True

            # 2. Chronological Catch-Up Replay for subsequent signals
            subsequent_msgs = channel_messages[matched_channel][matched_entry_idx + 1:]
            applied_updates = []
            
            for sub_msg in subsequent_msgs:
                sub_id = sub_msg.get("id", "")
                sub_text = sub_msg.get("text", "")
                if sub_id:
                    self.processed_ids[f"{matched_channel}:{sub_id}"] = True
                    
                sub_sig = parse_signal(sub_text)
                sub_type = sub_sig.get("type")
                if sub_type == SignalType.UNKNOWN:
                    continue
                    
                sub_sym = sub_sig.get("symbol", "").upper()
                if sub_sym:
                    base_sub = exec_obj._get_base_symbol(sub_sym)
                    base_pos = exec_obj._get_base_symbol(symbol.upper())
                    if base_sub != base_pos:
                        continue

                # Handle SL Update
                if sub_type == SignalType.SL_UPDATE:
                    is_be = sub_sig.get("breakeven", False)
                    target_sl = pos_price if is_be else float(sub_sig.get("sl", 0.0))
                    current_sl = float(pos.get("sl", 0.0))
                    pip_size = exec_obj._get_pip_size(symbol)
                    if target_sl > 0 and abs(current_sl - target_sl) > (pip_size * 0.5):
                        res_msg = exec_obj.execute_signal(sub_sig, channel=matched_channel, config=cfg)
                        applied_updates.append(f"SL updated: {res_msg}")
                        pos["sl"] = target_sl
                        if tg_token and tg_chat_id:
                            send_telegram_message(tg_token, tg_chat_id, f"🔄 [#{matched_channel}] Catch-up SL Reconciled for #{ticket}:\n{res_msg}")

                # Handle TP Update
                elif sub_type == SignalType.TP_UPDATE:
                    target_tp = float(sub_sig.get("tp", 0.0))
                    current_tp = float(pos.get("tp", 0.0))
                    if abs(current_tp - target_tp) > 0.01:
                        res_msg = exec_obj.execute_signal(sub_sig, channel=matched_channel, config=cfg)
                        applied_updates.append(f"TP updated: {res_msg}")
                        pos["tp"] = target_tp
                        if tg_token and tg_chat_id:
                            send_telegram_message(tg_token, tg_chat_id, f"🔄 [#{matched_channel}] Catch-up TP Reconciled for #{ticket}:\n{res_msg}")

                # Handle Close Position
                elif sub_type == SignalType.CLOSE_POSITION:
                    res_msg = exec_obj.execute_signal(sub_sig, channel=matched_channel, config=cfg)
                    applied_updates.append(f"Position closed: {res_msg}")
                    if tg_token and tg_chat_id:
                        send_telegram_message(tg_token, tg_chat_id, f"🔄 [#{matched_channel}] Catch-up Close Reconciled for #{ticket}:\n{res_msg}")

            adopted_results.append({
                "ticket": ticket,
                "symbol": symbol,
                "type": pos_type,
                "price_open": pos_price,
                "channel": matched_channel,
                "applied_updates": applied_updates
            })

        return {
            "success": True,
            "adopted_count": len(adopted_results),
            "adopted": adopted_results,
            "message": f"Successfully reconciled and adopted {len(adopted_results)} trade(s)."
        }

    def manually_adopt_trade(self, ticket: int, channel: str, symbol: Optional[str] = None) -> Dict[str, Any]:
        """
        Manually marks an open MT5 position or pending order as the active trade
        for a specified Discord signal channel.
        """
        ticket = int(ticket)
        chan_clean = channel.strip().lstrip("#").lower()
        exec_obj = self.get_executor()
        
        all_positions = self.get_open_positions()
        target_pos = None
        for p in all_positions:
            if p.get("ticket") == ticket:
                target_pos = p
                break
                
        if not target_pos:
            return {
                "success": False,
                "message": f"Position #{ticket} not found on any active MT5 terminal."
            }
            
        pos_symbol = target_pos.get("symbol", symbol or "")
        pos_type = target_pos.get("type", "BUY")
        pos_price = float(target_pos.get("price_open", 0.0))
        pos_volume = float(target_pos.get("volume", 0.0))
        pos_profit = float(target_pos.get("profit", 0.0))
        
        exec_obj.adopt_ticket(
            ticket,
            symbol=pos_symbol,
            channel=chan_clean,
            metadata={
                "manual": True,
                "channel": chan_clean,
                "pos_type": pos_type,
                "entry_price": pos_price,
                "volume": pos_volume,
                "adopted_at": time.time()
            }
        )
        
        log_trade_event(
            "ADOPT",
            pos_symbol,
            pos_type,
            pos_price,
            pos_volume,
            ticket,
            pos_profit,
            f"Manually marked as current trade for #{chan_clean}",
            provider=chan_clean
        )
        
        cfg = self.get_config()
        tg_token = cfg.get("TG_TOKEN")
        tg_chat_id = cfg.get("TG_CHAT_ID")
        if tg_token and tg_chat_id:
            formatted_msg = (
                f"🎯 [#{chan_clean} - TRADE MARKED AS CURRENT]\n"
                f"Adopted MT5 Position #{ticket}: {pos_type} {pos_symbol} @ {pos_price}\n"
                f"Bot is now actively managing this trade for incoming #{chan_clean} signals (Move SL to BE, SL updates, TP updates, and Close)."
            )
            send_telegram_message(tg_token, tg_chat_id, formatted_msg)
            
        logging.info(f"Position #{ticket} ({pos_symbol}) manually adopted for channel #{chan_clean}.")
        return {
            "success": True,
            "ticket": ticket,
            "channel": chan_clean,
            "symbol": pos_symbol,
            "message": f"Successfully marked #{ticket} as current trade for #{chan_clean}."
        }

    def manually_unadopt_trade(self, ticket: int) -> Dict[str, Any]:
        """
        Releases an adopted trade back to unmanaged status.
        """
        ticket = int(ticket)
        exec_obj = self.get_executor()
        meta = exec_obj.adopted_metadata.get(ticket, {})
        chan = meta.get("channel", "manual")
        sym = meta.get("symbol", "")
        
        exec_obj.unadopt_ticket(ticket)
        
        log_trade_event(
            "UNADOPT",
            sym,
            meta.get("pos_type", ""),
            meta.get("entry_price", 0.0),
            0.0,
            ticket,
            0.0,
            f"Unmanaged / released from #{chan} via Web UI",
            provider=chan
        )
        
        cfg = self.get_config()
        tg_token = cfg.get("TG_TOKEN")
        tg_chat_id = cfg.get("TG_CHAT_ID")
        if tg_token and tg_chat_id:
            formatted_msg = f"🔓 [TRADE UNLINKED]\nPosition #{ticket} ({sym}) is no longer managed by AutoTrader."
            send_telegram_message(tg_token, tg_chat_id, formatted_msg)
            
        logging.info(f"Position #{ticket} unadopted and released.")
        return {
            "success": True,
            "ticket": ticket,
            "message": f"Position #{ticket} unlinked and returned to unmanaged status."
        }

    def start_bot(self) -> Dict[str, Any]:
        """Start the background AutoTrader worker."""
        if self.is_running:
            return {"success": False, "message": "Bot is already running"}
            
        load_dotenv(override=True)
        ntfy_topic = os.getenv("NTFY_TOPIC", "").strip()
        if ntfy_topic and not ntfy_topic.startswith("http://") and not ntfy_topic.startswith("https://"):
            ntfy_topic = f"https://ntfy.sh/{ntfy_topic}"
        tg_token = os.getenv("TG_TOKEN")
        tg_chat_id = os.getenv("TG_CHAT_ID")
        lot_size = float(os.getenv("LOT_SIZE", "0.01"))
        
        if self.trigger_mode == "ntfy" and not ntfy_topic:
            return {"success": False, "message": "NTFY_TOPIC is required when in ntfy trigger mode."}
            
        self.stop_event.clear()
        self.is_running = True
        
        def run_loop():
            active_target = getattr(self, "active_trader", "all").upper()
            logging.info(f"AutoTrader started in [{self.trigger_mode.upper()}] mode (Active Target: {active_target}).")
            
            if self.trigger_mode == "polling":
                # CONTINUOUS BROWSER POLLING MODE (TESTING)
                logging.info("Polling browser tabs for new messages every 1.5s...")
                
                while not self.stop_event.is_set():
                    try:
                        active = getattr(self, "active_trader", "all").lower()
                        if active == "provider_a":
                            target_channels = ["channel-a"]
                        elif active == "provider_b":
                            target_channels = ["channel-b"]
                        elif active == "provider_c":
                            target_channels = ["channel-c"]
                        elif active == "provider_d":
                            target_channels = ["provider_d"]
                        else:
                            target_channels = ["channel-a", "channel-b", "channel-c", "provider_d"]

                        for channel in target_channels:
                            if self.stop_event.is_set():
                                break
                            recent_msgs, detected = extract_recent_discord_messages(channel_keyword=channel, limit=5)
                            if not recent_msgs:
                                continue
                                
                            for msg_obj in recent_msgs:
                                msg_id = msg_obj.get("id", "")
                                text = msg_obj.get("text", "")
                                if not msg_id or not text:
                                    continue
                                    
                                dedup_key = f"{channel}:{msg_id}"
                                if dedup_key in self.processed_ids:
                                    continue
                                    
                                signal = parse_signal(text)
                                if signal["type"] == SignalType.UNKNOWN:
                                    self.processed_ids[dedup_key] = True
                                    if len(self.processed_ids) > 100:
                                        self.processed_ids.popitem(last=False)
                                    continue
                                    
                                is_close_or_cancel = signal["type"] in (SignalType.CLOSE_POSITION, SignalType.CANCEL_PENDING)
                                max_age = 600.0 if is_close_or_cancel else 300.0
                                
                                if not self._is_message_valid(msg_id, max_age=max_age):
                                    self.processed_ids[dedup_key] = True
                                    if len(self.processed_ids) > 100:
                                        self.processed_ids.popitem(last=False)
                                    continue
                                        
                                self.processed_ids[dedup_key] = True
                                if len(self.processed_ids) > 100:
                                    self.processed_ids.popitem(last=False)

                                logging.info(f"[#{channel}] New Discord message detected: {text}")
                                logging.info(f"[#{channel}] Parsed Signal: {signal}")
                                exec_obj = self.get_executor()
                                try:
                                    result_msg = exec_obj.execute_signal(signal, channel=channel, config=self.get_config())
                                except Exception as e:
                                    result_msg = f"Execution Exception: {e}"
                                    logging.error(f"[#{channel}] {result_msg}", exc_info=True)

                                logging.info(f"[#{channel}] {result_msg}")
                                
                                if tg_token and tg_chat_id:
                                    is_error = isinstance(result_msg, str) and any(kw in result_msg.lower() for kw in [
                                        "error:", "trade failed", "close failed", "update failed", "rejected", "cannot", "security halt", "exception"
                                    ])
                                    if is_error:
                                        send_telegram_error(
                                            result_msg,
                                            context=f"Polling Trade Issue (#{channel})",
                                            token=tg_token,
                                            chat_id=tg_chat_id,
                                            force=True
                                        )
                                    else:
                                        formatted_msg = f"🔔 [#{channel}]\n{result_msg}"
                                        send_telegram_message(tg_token, tg_chat_id, formatted_msg)
                        
                        # Background sync for MT5 SL/TP exits
                        try:
                            from src.journal import sync_journal_with_mt5
                            sync_journal_with_mt5()
                        except Exception:
                            pass
                        
                        time.sleep(1.5)
                    except Exception as e:
                        if self.stop_event.is_set():
                            break
                        logging.warning(f"Polling loop error: {e}")
                        time.sleep(2)
                        
            else:
                # NTFY PUSH STREAM MODE (PRODUCTION)
                retry_count = 0
                while not self.stop_event.is_set():
                    try:
                        logging.info(f"Connecting to ntfy stream: {ntfy_topic}")
                        resp = requests.get(f"{ntfy_topic}/json", stream=True, timeout=(15, 300))
                        for line in resp.iter_lines():
                            if self.stop_event.is_set():
                                break
                            if line:
                                import json
                                try:
                                    data = json.loads(line)
                                except Exception:
                                    continue
                                if data.get("event") == "message":
                                    self._handle_notification(data, tg_token, tg_chat_id, lot_size)
                    except Exception as e:
                        if self.stop_event.is_set():
                            break
                        retry_count += 1
                        delay = min(5 * (2 ** (retry_count - 1)), 60)
                        logging.warning(f"Ntfy connection error: {e}. Reconnecting in {delay}s...")
                        if retry_count in (3, 6):
                            send_telegram_error(f"Ntfy stream disconnected: {e}. Reconnecting in {delay}s...", context="Ntfy Stream", token=tg_token, chat_id=tg_chat_id)
                        time.sleep(delay)
                        
            self.is_running = False
            logging.info("AutoTrader background worker stopped.")
            
        self.bot_thread = threading.Thread(target=run_loop, daemon=True)
        self.bot_thread.start()
        return {"success": True, "message": f"AutoTrader started in {self.trigger_mode.upper()} mode"}

    def stop_bot(self, wait: bool = False) -> Dict[str, Any]:
        """Stop the background AutoTrader listener."""
        if not self.is_running:
            return {"success": False, "message": "Bot is not running"}
            
        self.stop_event.set()
        self.is_running = False
        if wait and self.bot_thread and self.bot_thread.is_alive():
            self.bot_thread.join(timeout=3.0)
        logging.info("AutoTrader stop requested.")
        return {"success": True, "message": "AutoTrader stopping..."}

    def _handle_notification(self, data: dict, tg_token: str, tg_chat_id: str, lot_size: float):
        try:
            from main import identify_channel
            target_channel = identify_channel(data)
            if not target_channel:
                title = data.get("title", "")
                logging.info(f"Ignored notification from unapproved sender (title: '{title}').")
                return
                
            active = getattr(self, "active_trader", "all").lower()
            if active == "provider_a" and target_channel not in ["channel-a", "channel-a"]:
                logging.info(f"Ignored #{target_channel} notification (Bot is set to provider_a only).")
                return
            if active == "provider_b" and target_channel != "channel-b":
                logging.info(f"Ignored #{target_channel} notification (Bot is set to provider_b only).")
                return
            if active == "provider_c" and target_channel not in ["channel-c", "provider_c"]:
                logging.info(f"Ignored #{target_channel} notification (Bot is set to provider_c only).")
                return
            if active == "provider_d" and target_channel not in ["provider_d", "🦅-provider_d", "provider_d_alias"]:
                logging.info(f"Ignored #{target_channel} notification (Bot is set to provider_d only).")
                return

            logging.info(f"Matched channel #{target_channel}. Extracting Discord message...")
            
            # Retry extraction to handle transient CDP timeouts that would otherwise lose signals
            recent_msgs = []
            matched_channel = ""
            for extract_attempt in range(2):
                recent_msgs, matched_channel = extract_recent_discord_messages(channel_keyword=target_channel, limit=10)
                if recent_msgs:
                    break
                if extract_attempt == 0:
                    logging.warning(f"No text extracted from #{target_channel} on attempt 1, retrying in 1.5s...")
                    time.sleep(1.5)
            
            if not recent_msgs:
                warn_msg = f"No text extracted from #{target_channel} after 2 attempts. Discord tab may be inactive or closed!"
                logging.warning(warn_msg)
                if tg_token and tg_chat_id:
                    send_telegram_error(warn_msg, context=f"Discord Extractor (#{target_channel})", token=tg_token, chat_id=tg_chat_id)
                return

            ntfy_time = data.get("time") # UNIX timestamp from push notification

            for msg_obj in recent_msgs:
                msg_id = msg_obj.get("id", "")
                text = msg_obj.get("text", "")
                if not msg_id or not text:
                    continue

                dedup_key = f"{target_channel}:{msg_id}"
                if dedup_key in self.processed_ids:
                    continue

                signal = parse_signal(text)
                if signal["type"] == SignalType.UNKNOWN:
                    self.processed_ids[dedup_key] = True
                    if len(self.processed_ids) > 100:
                        self.processed_ids.popitem(last=False)
                    logging.info(f"[#{target_channel}] Message parsed as UNKNOWN. Ignoring.")
                    continue

                is_close_or_cancel = signal["type"] in (SignalType.CLOSE_POSITION, SignalType.CANCEL_PENDING)
                max_age = 600.0 if is_close_or_cancel else 300.0

                if not self._is_message_valid(msg_id, ntfy_time=ntfy_time, max_age=max_age):
                    self.processed_ids[dedup_key] = True
                    if len(self.processed_ids) > 100:
                        self.processed_ids.popitem(last=False)
                    continue

                self.processed_ids[dedup_key] = True
                if len(self.processed_ids) > 100:
                    self.processed_ids.popitem(last=False)

                logging.info(f"[#{target_channel}] Extracted: {text}")
                logging.info(f"[#{target_channel}] Parsed Signal: {signal}")
                exec_obj = self.get_executor()
                try:
                    result_msg = exec_obj.execute_signal(signal, channel=target_channel, config=self.get_config())
                except Exception as e:
                    result_msg = f"Execution Exception: {e}"
                    logging.error(f"[#{target_channel}] {result_msg}", exc_info=True)

                logging.info(f"[#{target_channel}] {result_msg}")

                if tg_token and tg_chat_id:
                    is_error = isinstance(result_msg, str) and any(kw in result_msg.lower() for kw in [
                        "error:", "trade failed", "close failed", "update failed", "rejected", "cannot", "security halt", "exception"
                    ])
                    if is_error:
                        send_telegram_error(
                            result_msg,
                            context=f"Signal Execution Issue (#{target_channel})",
                            token=tg_token,
                            chat_id=tg_chat_id,
                            force=True
                        )
                    else:
                        formatted_msg = f"🔔 [#{target_channel}]\n{result_msg}"
                        send_telegram_message(tg_token, tg_chat_id, formatted_msg)
        except Exception as e:
            err_msg = f"Error handling notification: {e}"
            logging.error(err_msg, exc_info=True)
            if tg_token and tg_chat_id:
                send_telegram_error(err_msg, context="Notification Handler", token=tg_token, chat_id=tg_chat_id)
