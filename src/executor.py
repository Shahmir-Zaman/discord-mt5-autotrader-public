import MetaTrader5 as mt5
import logging
import os
import threading
import time
from typing import Dict, Any, Optional, List, Set
from src.journal import log_trade_event
from src.parser import SignalType

# Reentrant lock to serialize all MT5 operations across background worker and web server threads
MT5_LOCK = threading.RLock()

class MT5Executor:
    PIP_SIZES = {
        "XAUUSD": 0.1,
    }
    DEFAULT_PIP_SIZE = 0.0001

    def __init__(self, magic_number: int = 123456, pip_thresholds: Dict[str, int] = None, symbol_suffix: str = None):
        self.magic_number = magic_number
        self.pip_thresholds = pip_thresholds or {"XAUUSD": 50}
        self.symbol_suffix = symbol_suffix if symbol_suffix is not None else os.environ.get("SYMBOL_SUFFIX", "")
        self.active_breakeven_triggers: List[Dict[str, Any]] = []
        self._watcher_running = False
        self._watcher_thread: Optional[threading.Thread] = None
        self._recent_trades: Dict[str, float] = {}
        self.adopted_tickets: Set[int] = set()
        self.adopted_metadata: Dict[int, Dict[str, Any]] = {}
        self.ticket_channel: Dict[int, str] = {}
        with MT5_LOCK:
            if not mt5.initialize():
                logging.error(f"MT5 init failed: {mt5.last_error()}")

    def adopt_ticket(self, ticket: int, symbol: str = "", channel: str = "", metadata: Dict[str, Any] = None):
        """Adopt an existing open position or order into the bot's managed registry."""
        t_id = int(ticket)
        self.adopted_tickets.add(t_id)
        meta = dict(metadata or {})
        meta.update({
            "ticket": t_id,
            "symbol": symbol,
            "channel": channel,
            "adopted_at": time.time()
        })
        self.adopted_metadata[t_id] = meta
        if channel:
            self.ticket_channel[t_id] = channel
        logging.info(f"Adopted ticket #{t_id} ({symbol}) for management under channel '{channel}'.")

    def unadopt_ticket(self, ticket: int):
        """Remove a ticket from the adopted registry upon close/cancel."""
        t_id = int(ticket)
        self.adopted_tickets.discard(t_id)
        self.adopted_metadata.pop(t_id, None)
        self.ticket_channel.pop(t_id, None)

    def is_ticket_managed(self, ticket: int, magic: Optional[int] = None) -> bool:
        """Check if a ticket is managed either via bot magic number or adoption."""
        if magic is not None and magic == self.magic_number:
            return True
        if int(ticket) in self.adopted_tickets:
            return True
        return False

    def get_adopted_tickets(self) -> List[Dict[str, Any]]:
        """Return list of metadata for all currently adopted tickets."""
        return list(self.adopted_metadata.values())
            
    def shutdown(self):
        with MT5_LOCK:
            mt5.shutdown()
        
    def _get_base_symbol(self, symbol: str) -> str:
        for base in self.PIP_SIZES.keys():
            if symbol.startswith(base):
                return base
        return symbol

    def _get_pip_size(self, symbol: str) -> float:
        base = self._get_base_symbol(symbol)
        return self.PIP_SIZES.get(symbol, self.PIP_SIZES.get(base, self.DEFAULT_PIP_SIZE))

    def _get_pip_threshold(self, symbol: str) -> Optional[int]:
        base = self._get_base_symbol(symbol)
        return self.pip_thresholds.get(symbol, self.pip_thresholds.get(base))

    def _resolve_symbol(self, raw_symbol: str, config: Dict[str, str] = None, channel: str = "") -> str:
        if not raw_symbol:
            return raw_symbol
            
        cfg = config or {}
        chan = (channel or getattr(self, "current_channel", "") or "").lower()
        target_mode = (cfg.get("TARGET_TERMINAL") or os.getenv("TARGET_TERMINAL", "auto")).strip().lower()
        
        is_provider_d = any(kw in chan for kw in ["provider_d", "🦅", "provider_d_alias"])
        provider_d_target = (cfg.get("PROVIDER_D_TARGET_TERMINAL") or os.getenv("PROVIDER_D_TARGET_TERMINAL", "terminal_1")).strip().lower()
        if is_provider_d and target_mode in ["auto", ""]:
            target_mode = provider_d_target

        is_terminal_owner = (target_mode in ["terminal_3", "terminal_owner"])
        if is_terminal_owner:
            is_provider_b = False
        elif target_mode in ["terminal_2", "provider_b"]:
            is_provider_b = True
        elif target_mode in ["terminal_1", "provider_a"]:
            is_provider_b = False
        else:
            is_provider_b = any(kw in chan for kw in ["provider_b", "channel-b", "provider_b", "fx", "provider_c", "channel-c"])
        
        # Determine target suffix: prioritize terminal/channel-specific setting, then fallback to global
        suffix = None
        # 1. First check explicit keys in cfg dict
        if is_provider_d and "PROVIDER_D_SYMBOL_SUFFIX" in cfg:
            suffix = cfg.get("PROVIDER_D_SYMBOL_SUFFIX")
        elif is_terminal_owner and "TERMINAL_OWNER_SYMBOL_SUFFIX" in cfg:
            suffix = cfg.get("TERMINAL_OWNER_SYMBOL_SUFFIX")
        elif is_provider_b and "PROVIDER_B_SYMBOL_SUFFIX" in cfg:
            suffix = cfg.get("PROVIDER_B_SYMBOL_SUFFIX")
        elif not (is_provider_d or is_terminal_owner or is_provider_b) and "PROVIDER_A_SYMBOL_SUFFIX" in cfg:
            suffix = cfg.get("PROVIDER_A_SYMBOL_SUFFIX")
        elif "SYMBOL_SUFFIX" in cfg:
            suffix = cfg.get("SYMBOL_SUFFIX")
            
        # 2. Fall back to os.environ or instance default if not explicitly provided in cfg
        if suffix is None:
            if is_provider_d and os.getenv("PROVIDER_D_SYMBOL_SUFFIX") is not None:
                suffix = os.getenv("PROVIDER_D_SYMBOL_SUFFIX")
            elif is_terminal_owner and os.getenv("TERMINAL_OWNER_SYMBOL_SUFFIX") is not None:
                suffix = os.getenv("TERMINAL_OWNER_SYMBOL_SUFFIX")
            elif is_provider_b and os.getenv("PROVIDER_B_SYMBOL_SUFFIX") is not None:
                suffix = os.getenv("PROVIDER_B_SYMBOL_SUFFIX")
            elif not (is_provider_d or is_terminal_owner or is_provider_b) and os.getenv("PROVIDER_A_SYMBOL_SUFFIX") is not None:
                suffix = os.getenv("PROVIDER_A_SYMBOL_SUFFIX")
            elif hasattr(self, "symbol_suffix") and self.symbol_suffix is not None:
                suffix = self.symbol_suffix
            else:
                suffix = os.getenv("SYMBOL_SUFFIX", "")
                
        suffix = str(suffix).strip() if suffix else ""
        
        # 1. If a suffix is explicitly configured and raw_symbol doesn't end with it, check suffixed symbol first
        if suffix and not raw_symbol.endswith(suffix):
            suffixed = f"{raw_symbol}{suffix}"
            if mt5.symbol_select(suffixed, True):
                return suffixed
            # Fallback to checking raw_symbol if suffixed symbol not found in MT5
            if mt5.symbol_select(raw_symbol, True):
                return raw_symbol
            return suffixed
            
        # 2. Check if raw symbol is directly selected/supported in MT5
        if mt5.symbol_select(raw_symbol, True):
            return raw_symbol
            
        return raw_symbol

    def _ensure_terminal_for_channel(self, channel: str = "", config: Dict[str, str] = None) -> bool:
        """Binds MT5 Python connection to the appropriate terminal based on configuration and channel."""
        channel_lower = (channel or getattr(self, "current_channel", "") or "").lower()
        cfg = config or {}
        target_mode = (cfg.get("TARGET_TERMINAL") or os.getenv("TARGET_TERMINAL", "auto")).strip()
        
        provider_a_default = r"C:\Program Files\MetaTrader 5\terminal64.exe"
        provider_b_default = r"C:\Program Files\MetaTrader 5 - provider_b\terminal64.exe"
        terminal_owner_default = r"C:\Program Files\MetaTrader 5 - terminal_owner\terminal64.exe"
        provider_a_path = cfg.get("PROVIDER_A_MT5_PATH") or os.getenv("PROVIDER_A_MT5_PATH", provider_a_default)
        provider_b_path = cfg.get("PROVIDER_B_MT5_PATH") or os.getenv("PROVIDER_B_MT5_PATH", provider_b_default)
        terminal_owner_path = cfg.get("TERMINAL_OWNER_MT5_PATH") or os.getenv("TERMINAL_OWNER_MT5_PATH", terminal_owner_default)
        
        is_provider_d = any(kw in channel_lower for kw in ["provider_d", "🦅", "provider_d_alias"])
        provider_d_target = (cfg.get("PROVIDER_D_TARGET_TERMINAL") or os.getenv("PROVIDER_D_TARGET_TERMINAL", "terminal_1")).strip().lower()
        
        target_mode_lower = target_mode.lower()
        if is_provider_d and target_mode_lower in ["auto", ""]:
            target_mode_lower = provider_d_target

        is_provider_b = any(kw in channel_lower for kw in ["provider_b", "channel-b", "provider_b", "fx", "provider_c", "channel-c"])
        terminal_label = "provider_b" if is_provider_b else "provider_a"
        
        if target_mode_lower in ["terminal_1", "provider_a"]:
            target_path = provider_a_path
            terminal_label = "Terminal 1 (Dedicated - provider_a)" if not is_provider_d else "Terminal 1 (provider_d - #🦅-provider_d)"
            is_provider_b = False
        elif target_mode_lower in ["terminal_2", "provider_b"]:
            target_path = provider_b_path
            terminal_label = "Terminal 2 (Dedicated - provider_b)" if not is_provider_d else "Terminal 2 (provider_d - #🦅-provider_d)"
            is_provider_b = True
        elif target_mode_lower in ["terminal_3", "terminal_owner"]:
            target_path = terminal_owner_path
            terminal_label = "Terminal 3 (Dedicated - terminal_owner)" if not is_provider_d else "Terminal 3 (provider_d - #🦅-provider_d)"
            is_provider_b = False
        elif target_mode.lower().endswith(".exe") and os.path.exists(target_mode):
            target_path = target_mode
            terminal_label = f"Custom ({os.path.basename(target_mode)})"
        else:
            # Auto channel routing
            if is_provider_d:
                target_path = provider_a_path
                terminal_label = "provider_d (#🦅-provider_d -> Terminal 1)"
            elif is_provider_b:
                target_path = provider_b_path
                terminal_label = "provider_b (#channel-b / #channel-c)"
            else:
                target_path = provider_a_path
                terminal_label = "provider_a (#channel-a)"
            
        target_path = target_path.strip() if target_path else ""
        if not target_path or not os.path.exists(target_path):
            if not mt5.initialize():
                logging.error(f"MT5 fallback init failed: {mt5.last_error()}")
                return False
            return True
            
        # Always re-initialize to target_path; do NOT skip via cached path because other threads or queries switch MT5 globally
        ok = mt5.initialize(path=target_path)
        if ok:
            self._active_terminal_path = target_path
            acc = mt5.account_info()
            login_id = acc.login if acc else "Unknown"
            
            # Optional login validation safeguard
            expected_login = (cfg.get("PROVIDER_B_MT5_LOGIN") or os.getenv("PROVIDER_B_MT5_LOGIN")) if is_provider_b else (cfg.get("PROVIDER_A_MT5_LOGIN") or os.getenv("PROVIDER_A_MT5_LOGIN"))
            if expected_login and acc and hasattr(acc, "login") and isinstance(getattr(acc, "login", None), int):
                expected_str = str(expected_login).strip()
                if expected_str.isdigit() and acc.login != int(expected_str):
                    logging.error(f"SECURITY HALT: Active MT5 login #{acc.login} does NOT match expected #{expected_str} for {terminal_label}! Order blocked.")
                    return False

            logging.info(f"Switched MT5 context to {terminal_label} (Login: {login_id}, Path: {target_path})")
            return True
        else:
            logging.error(f"Failed to switch MT5 context to {target_path}: {mt5.last_error()}")
            return False

    def execute_signal(self, signal: Dict[str, Any], channel: str = "", config: Dict[str, str] = None) -> str:
        with MT5_LOCK:
            self.current_channel = channel
            if config is None:
                config = {}
                
            try:
                if not self._ensure_terminal_for_channel(channel, config):
                    err = f"Error: Could not bind MT5 terminal context for channel '{channel}'"
                    logging.error(f"[{channel}] {err}")
                    return err
                    
                raw_symbol = signal.get("symbol", "")
                resolved_symbol = self._resolve_symbol(raw_symbol, config, channel=channel)
                
                # Create shallow copy of signal with resolved symbol and preserve raw_symbol
                sig = dict(signal)
                sig["symbol"] = resolved_symbol
                sig["raw_symbol"] = raw_symbol
                
                if sig["type"].value == "ENTRY":
                    res = self._open_trade(sig, config)
                elif sig["type"].value == "SL_UPDATE":
                    res = self._update_sl(sig)
                elif sig["type"].value == "TP_UPDATE":
                    res = self._update_tp(sig)
                elif sig["type"].value == "CLOSE_POSITION":
                    res = self._close_position(sig)
                elif sig["type"].value == "CANCEL_PENDING":
                    res = self._cancel_pending(sig)
                elif sig["type"].value == "BREAKEVEN_TRIGGER":
                    res = self._handle_breakeven_trigger(sig, config)
                else:
                    res = "Unknown or unhandled signal type"

                # If result indicates a trade failure or rejection, log at ERROR level
                if isinstance(res, str) and any(err_kw in res.lower() for err_kw in [
                    "error:", "trade failed", "close failed", "update failed", "rejected", "failed to move sl"
                ]):
                    logging.error(f"[{channel}] Trade Execution Issue: {res}")

                return res
            except Exception as e:
                err_msg = f"Exception executing signal in #{channel}: {e}"
                logging.error(err_msg, exc_info=True)
                return f"Execution Exception: {err_msg}"

        
    SYMBOL_FILLING_FOK = 1
    SYMBOL_FILLING_IOC = 2

    def _get_filling_mode(self, symbol: str) -> int:
        info = mt5.symbol_info(symbol)
        if info is None:
            return getattr(mt5, "ORDER_FILLING_IOC", 1)
        filling = getattr(info, "filling_mode", 0)
        if filling & self.SYMBOL_FILLING_FOK:
            return getattr(mt5, "ORDER_FILLING_FOK", 0)
        elif filling & self.SYMBOL_FILLING_IOC:
            return getattr(mt5, "ORDER_FILLING_IOC", 1)
        return getattr(mt5, "ORDER_FILLING_RETURN", getattr(mt5, "ORDER_FILLING_IOC", 1))

    @staticmethod
    def calculate_MILESTONE_risk(balance: float, starting_milestone: float = 100.0) -> Dict[str, Any]:
        """
        Calculates risk allocation using the ExampleStrategy Money Management Pro model (MILESTONE).
        - Milestone doubles when balance >= 2 * Milestone (starting from starting_milestone).
        - Base Risk = 5% of Milestone.
        - Profit Above Milestone = max(0, balance - Milestone).
        - Tiered Profit Boost:
            Tier 1: 15% if balance < 1.5 * Milestone (cycle gain < 50%)
            Tier 2: 12% if balance < 1.8 * Milestone (cycle gain 50% to < 80%)
            Tier 3: 9%  if balance >= 1.8 * Milestone (cycle gain >= 80%)
        - Total Risk = Base Risk + Profit Boost.
        """
        bal = max(0.0, float(balance))
        start_m = max(1.0, float(starting_milestone))
        
        milestone = start_m
        while bal >= milestone * 2.0:
            milestone *= 2.0
            
        base_risk = milestone * 0.05
        profit_above = max(0.0, bal - milestone)
        
        tier = 1
        boost_rate = 0.15
        if bal >= milestone * 1.8:
            tier = 3
            boost_rate = 0.09
        elif bal >= milestone * 1.5:
            tier = 2
            boost_rate = 0.12
            
        boost = profit_above * boost_rate
        total_risk = base_risk + boost
        effective_risk_pct = (total_risk / bal * 100.0) if bal > 0 else 5.0
        
        return {
            "balance": bal,
            "milestone": milestone,
            "base_risk": round(base_risk, 2),
            "profit_above_milestone": round(profit_above, 2),
            "tier": tier,
            "boost_rate": boost_rate,
            "boost": round(boost, 2),
            "total_risk": round(total_risk, 2),
            "effective_risk_pct": round(effective_risk_pct, 2)
        }

    def _calculate_lot_from_money(self, symbol: str, entry_price: float, sl_price: float, risk_money: float, fallback_lot: float = 0.01) -> float:
        info = mt5.symbol_info(symbol)
        if not info:
            return fallback_lot
            
        tick_size = getattr(info, "trade_tick_size", None)
        tick_val = getattr(info, "trade_tick_value", None)
        try:
            if not tick_size or float(tick_size) <= 0 or not tick_val or float(tick_val) <= 0:
                return fallback_lot
        except (TypeError, ValueError):
            return fallback_lot
            
        ticks_lost = abs(entry_price - sl_price) / tick_size
        loss_per_lot = ticks_lost * tick_val
        
        if loss_per_lot <= 0:
            return float(getattr(info, "volume_min", 0.01))
            
        try:
            calculated_lot = risk_money / loss_per_lot
            step = float(info.volume_step) if getattr(info, "volume_step", None) else 0.01
            calculated_lot = round(calculated_lot / step) * step
            
            vol_min = float(info.volume_min) if getattr(info, "volume_min", None) else 0.01
            vol_max = float(info.volume_max) if getattr(info, "volume_max", None) else 100.0
            if calculated_lot < vol_min:
                calculated_lot = vol_min
            if calculated_lot > vol_max:
                calculated_lot = vol_max
                
            return round(calculated_lot, 2)
        except (TypeError, ValueError):
            return fallback_lot

    def _calculate_lot_size(self, symbol: str, entry_price: float, sl_price: float, risk_pct: float, fixed_fallback: float) -> float:
        info = mt5.symbol_info(symbol)
        if not info:
            return fixed_fallback
            
        tick_size = getattr(info, "trade_tick_size", None)
        tick_val = getattr(info, "trade_tick_value", None)
        try:
            if not tick_size or float(tick_size) <= 0 or not tick_val or float(tick_val) <= 0:
                return fixed_fallback
        except (TypeError, ValueError):
            return fixed_fallback
            
        account = mt5.account_info()
        if not account:
            return info.volume_min
            
        risk_money = account.balance * (risk_pct / 100.0)
        
        ticks_lost = abs(entry_price - sl_price) / tick_size
        loss_per_lot = ticks_lost * tick_val
        
        if loss_per_lot == 0:
            return info.volume_min
            
        try:
            calculated_lot = risk_money / loss_per_lot
            step = float(info.volume_step) if getattr(info, "volume_step", None) else 0.01
            calculated_lot = round(calculated_lot / step) * step
            
            vol_min = float(info.volume_min) if getattr(info, "volume_min", None) else 0.01
            vol_max = float(info.volume_max) if getattr(info, "volume_max", None) else 100.0
            if calculated_lot < vol_min:
                calculated_lot = vol_min
            if calculated_lot > vol_max:
                calculated_lot = vol_max
                
            return round(calculated_lot, 2)
        except (TypeError, ValueError):
            return fixed_fallback
        
    def _open_trade(self, signal: Dict[str, Any], config: Dict[str, str]) -> str:
        symbol = signal["symbol"]
        raw_symbol = signal.get("raw_symbol", symbol)
        action = signal["action"]
        order_type_str = signal.get("order_type", "MARKET")
        
        # 10-second duplicate execution prevention (prevents dual orders from multiple runners or duplicate webhooks)
        import time
        now = time.time()
        if not hasattr(self, "_recent_trades"):
            self._recent_trades = {}
        self._recent_trades = {k: v for k, v in self._recent_trades.items() if now - v < 60}
        
        channel_key = getattr(self, "current_channel", "default")
        dedup_key = f"{channel_key}:{symbol}:{action}"
        last_time = self._recent_trades.get(dedup_key)
        if last_time and (now - last_time) < 10.0:
            elapsed = now - last_time
            logging.warning(f"Duplicate order rejected: {symbol} {action} from #{channel_key} already executed {elapsed:.1f}s ago.")
            return f"Duplicate trade rejected: {symbol} {action} from #{channel_key} already executed {elapsed:.1f}s ago."
        self._recent_trades[dedup_key] = now
        
        if not mt5.symbol_select(symbol, True):
            if raw_symbol != symbol and mt5.symbol_select(raw_symbol, True):
                symbol = raw_symbol
            else:
                return f"Error: Symbol {symbol} not found"
        
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            return f"Error: Could not retrieve tick data for {symbol}"
            
        # Check if we should auto-close previous positions for this channel before opening a new trade
        auto_close_note = ""
        auto_close_prev_str = str(config.get("AUTO_CLOSE_PREVIOUS_ON_NEW_ENTRY", os.getenv("AUTO_CLOSE_PREVIOUS_ON_NEW_ENTRY", "true"))).lower()
        auto_close_prev = auto_close_prev_str in ["true", "1", "yes"]

        if auto_close_prev and order_type_str == "MARKET":
            existing_positions = self._find_all_positions(symbol, raw_symbol)
            closed_tickets_list = []
            for prev_pos in existing_positions:
                prev_ticket = getattr(prev_pos, "ticket", 0)
                prev_time = getattr(prev_pos, "time", None)
                # Ignore positions opened within the last 15s to allow intentional dual entries
                if isinstance(prev_time, (int, float)) and prev_time > 0 and (now - prev_time) < 15.0:
                    continue

                # Channel matching guard:
                meta = self.adopted_metadata.get(prev_ticket, {})
                pos_channel = meta.get("channel", "") or self.ticket_channel.get(prev_ticket, "")
                prev_magic = getattr(prev_pos, "magic", None)

                if pos_channel:
                    # If this position has an explicit channel assigned, it MUST match the incoming channel key
                    if channel_key and pos_channel.lower() != channel_key.lower():
                        continue
                else:
                    # Non-adopted / untracked position: only touch if it matches the bot's magic number
                    if prev_magic != self.magic_number:
                        continue

                logging.warning(
                    f"[{channel_key}] Auto-closing previous stale open position #{prev_ticket} "
                    f"({getattr(prev_pos, 'symbol', symbol)} {getattr(prev_pos, 'volume', 0.0)} lots) before executing new {action} trade."
                )

                # Close this specific ticket directly via mt5.order_send (avoids re-querying all positions)
                pos_symbol = getattr(prev_pos, "symbol", symbol)
                pos_tick = mt5.symbol_info_tick(pos_symbol) or tick
                pos_type = getattr(prev_pos, "type", 0)
                close_order_type = mt5.ORDER_TYPE_SELL if pos_type == 0 else mt5.ORDER_TYPE_BUY
                close_price = (pos_tick.bid if pos_type == 0 else pos_tick.ask) if pos_tick else (tick.bid if pos_type == 0 else tick.ask)

                close_request = {
                    "action": mt5.TRADE_ACTION_DEAL,
                    "symbol": pos_symbol,
                    "position": prev_ticket,
                    "volume": float(getattr(prev_pos, "volume", 0.01)),
                    "type": close_order_type,
                    "price": close_price,
                    "deviation": 20,
                    "magic": prev_magic or self.magic_number,
                    "comment": f"Auto-closed #{channel_key}",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": self._get_filling_mode(pos_symbol),
                }

                close_result = mt5.order_send(close_request)
                if close_result is not None and close_result.retcode == mt5.TRADE_RETCODE_DONE:
                    self.unadopt_ticket(prev_ticket)
                    self.ticket_channel.pop(prev_ticket, None)
                    log_trade_event(
                        "CLOSE", pos_symbol, "CLOSE", close_price,
                        float(getattr(prev_pos, "volume", 0.01)),
                        prev_ticket, float(getattr(prev_pos, "profit", 0.0)),
                        f"Auto-closed prior to new ENTRY from #{channel_key}",
                        provider=channel_key or getattr(self, "current_channel", "System")
                    )
                    closed_tickets_list.append(f"#{prev_ticket} (Closed @ {close_price})")
                    logging.info(f"[{channel_key}] Auto-closed stale position #{prev_ticket} successfully.")
                else:
                    err = close_result.comment if close_result else mt5.last_error()
                    logging.error(f"[{channel_key}] Failed to auto-close stale position #{prev_ticket}: {err}")
                    closed_tickets_list.append(f"#{prev_ticket} (Failed: {err})")

            if closed_tickets_list:
                auto_close_note = f"⚠️ Auto-closed previous trade(s): {', '.join(closed_tickets_list)}\n"

        # Determine entry price and MT5 order type
        if order_type_str == "MARKET":
            entry_price = tick.ask if action == "BUY" else tick.bid
            order_type = mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL
        else:
            entry_price = signal.get("price")
            if not entry_price:
                return "Error: Pending order missing price"
            if action == "BUY":
                order_type = mt5.ORDER_TYPE_BUY_LIMIT if order_type_str == "LIMIT" else mt5.ORDER_TYPE_BUY_STOP
            else:
                order_type = mt5.ORDER_TYPE_SELL_LIMIT if order_type_str == "LIMIT" else mt5.ORDER_TYPE_SELL_STOP
        
        # Determine active terminal and channel prefixes
        active_path = getattr(self, "_active_terminal_path", "") or ""
        active_lower = active_path.lower()
        target_mode = (config.get("TARGET_TERMINAL") or os.getenv("TARGET_TERMINAL", "auto")).strip().lower()
        
        # Match configured paths directly rather than relying on folder name substrings
        provider_a_cfg = (config.get("PROVIDER_A_MT5_PATH") or os.getenv("PROVIDER_A_MT5_PATH", "")).strip().lower()
        provider_b_cfg = (config.get("PROVIDER_B_MT5_PATH") or os.getenv("PROVIDER_B_MT5_PATH", "")).strip().lower()
        terminal_owner_cfg = (config.get("TERMINAL_OWNER_MT5_PATH") or os.getenv("TERMINAL_OWNER_MT5_PATH", "")).strip().lower()

        terminal_prefixes = []
        if active_lower and terminal_owner_cfg and os.path.normpath(active_lower) == os.path.normpath(terminal_owner_cfg):
            terminal_prefixes = ["TERMINAL_3", "TERMINAL_OWNER"]
        elif active_lower and provider_b_cfg and os.path.normpath(active_lower) == os.path.normpath(provider_b_cfg):
            terminal_prefixes = ["TERMINAL_2", "PROVIDER_B"]
        elif active_lower and provider_a_cfg and os.path.normpath(active_lower) == os.path.normpath(provider_a_cfg):
            terminal_prefixes = ["TERMINAL_1", "PROVIDER_A"]
        elif target_mode in ["terminal_3", "terminal_owner"]:
            terminal_prefixes = ["TERMINAL_3", "TERMINAL_OWNER"]
        elif target_mode in ["terminal_2", "provider_b"]:
            terminal_prefixes = ["TERMINAL_2", "PROVIDER_B"]
        elif target_mode in ["terminal_1", "provider_a"]:
            terminal_prefixes = ["TERMINAL_1", "PROVIDER_A"]
        elif "terminal_owner" in active_lower and not provider_b_cfg:
            terminal_prefixes = ["TERMINAL_3", "TERMINAL_OWNER"]
        elif "provider_b" in active_lower and not terminal_owner_cfg:
            terminal_prefixes = ["TERMINAL_2", "PROVIDER_B"]
        elif "metatrader 5" in active_lower:
            terminal_prefixes = ["TERMINAL_1", "PROVIDER_A"]

        # Channel-specific candidate prefixes (provider_a, provider_b, provider_d, provider_c)
        channel_name = getattr(self, "current_channel", "").lower()
        channel_upper = channel_name.upper().replace("-", "_")
        channel_prefixes = []
        if any(kw in channel_name for kw in ["provider_d", "🦅", "provider_d_alias"]):
            channel_prefixes.extend(["PROVIDER_D"])
        elif "provider_c" in channel_name:
            channel_prefixes.extend(["PROVIDER_C", "PROVIDER_C_FX", "PROVIDER_B", "FX_PROVIDER_B"])
        elif "provider_b" in channel_name or "insider" in channel_name or "fx" in channel_name:
            channel_prefixes.extend(["PROVIDER_B", "FX_PROVIDER_B"])
        elif "provider_a" in channel_name or "house" in channel_name:
            channel_prefixes.extend(["PROVIDER_A", "HOUSE_CHALLENGE", "PROVIDER_A_XAU"])
        elif "terminal_owner" in channel_name:
            channel_prefixes.extend(["TERMINAL_OWNER", "TERMINAL_3"])
        if channel_upper and channel_upper not in channel_prefixes:
            channel_prefixes.append(channel_upper)

        # Prioritize channel provider settings, falling back to active terminal settings
        prefixes = []
        for p in channel_prefixes + terminal_prefixes:
            if p not in prefixes:
                prefixes.append(p)
            
        use_risk = False
        use_MILESTONE = False
        risk_pct = 1.0
        fixed_lot = 0.01
        starting_milestone = 100.0
        active_prefix = None
        
        # First check prefixes explicitly present in the active config dict
        active_prefix = None
        for p in prefixes:
            if f"{p}_LOT_MODE" in config or f"{p}_FIXED_LOT" in config or f"{p}_RISK_PCT" in config:
                active_prefix = p
                break
                
        # If none found in config dict, check os.environ fallbacks
        if not active_prefix:
            for p in prefixes:
                if os.getenv(f"{p}_LOT_MODE") is not None or os.getenv(f"{p}_FIXED_LOT") is not None:
                    active_prefix = p
                    break

        if active_prefix:
            p = active_prefix
            lot_mode_key = f"{p}_LOT_MODE"
            raw_mode = str(config.get(lot_mode_key) if lot_mode_key in config else os.getenv(lot_mode_key, "")).strip().upper()
            use_MILESTONE = (raw_mode == "MILESTONE")
            use_risk = (raw_mode == "RISK")
            
            risk_pct_val = config.get(f"{p}_RISK_PCT") if f"{p}_RISK_PCT" in config else os.getenv(f"{p}_RISK_PCT", "1.0")
            try:
                risk_pct = float(risk_pct_val)
            except (ValueError, TypeError):
                risk_pct = 1.0
                
            fixed_lot_val = config.get(f"{p}_FIXED_LOT") if f"{p}_FIXED_LOT" in config else os.getenv(f"{p}_FIXED_LOT", "0.01")
            try:
                fixed_lot = float(fixed_lot_val)
            except (ValueError, TypeError):
                fixed_lot = 0.01
                
            sm_val = config.get(f"{p}_STARTING_MILESTONE") if f"{p}_STARTING_MILESTONE" in config else os.getenv(f"{p}_STARTING_MILESTONE", "100.0")
            try:
                starting_milestone = float(sm_val)
            except (ValueError, TypeError):
                starting_milestone = 100.0
            
        apply_signal_risk_to_fixed = True
        if config and "APPLY_SIGNAL_RISK_TO_FIXED_LOT" in config:
            apply_signal_risk_to_fixed = (str(config.get("APPLY_SIGNAL_RISK_TO_FIXED_LOT")).strip().lower() in ("true", "1", "yes"))
        elif config and "APPLY_SIGNAL_RISK_TO_FIXED" in config:
            apply_signal_risk_to_fixed = (str(config.get("APPLY_SIGNAL_RISK_TO_FIXED")).strip().lower() in ("true", "1", "yes"))
        elif os.getenv("APPLY_SIGNAL_RISK_TO_FIXED_LOT") is not None:
            apply_signal_risk_to_fixed = (os.getenv("APPLY_SIGNAL_RISK_TO_FIXED_LOT").strip().lower() in ("true", "1", "yes"))
        elif os.getenv("APPLY_SIGNAL_RISK_TO_FIXED") is not None:
            apply_signal_risk_to_fixed = (os.getenv("APPLY_SIGNAL_RISK_TO_FIXED").strip().lower() in ("true", "1", "yes"))

        risk_mult = float(signal.get("risk_multiplier", 1.0))
        if risk_mult <= 0:
            risk_mult = 1.0

        if risk_mult != 1.0:
            orig_risk = risk_pct
            risk_pct = risk_pct * risk_mult
            if apply_signal_risk_to_fixed:
                orig_lot = fixed_lot
                fixed_lot = round(fixed_lot * risk_mult, 2)
                logging.info(
                    f"[{channel_name or 'AutoTrader'}] Signal multiplier {risk_mult}x applied: "
                    f"risk_pct {orig_risk}% -> {risk_pct}%, fixed_lot {orig_lot} -> {fixed_lot}"
                )
            else:
                logging.info(
                    f"[{channel_name or 'AutoTrader'}] Signal multiplier {risk_mult}x applied to risk_pct "
                    f"({orig_risk}% -> {risk_pct}%). Fixed lot preserved at {fixed_lot} (Fixed Lot mode)."
                )
            
        sl_price = signal.get("sl")
        price2 = signal.get("price2")

        # Handle Dual Entry Limit orders (e.g. provider_b 75/25 or 50/50 split)
        if order_type_str != "MARKET" and price2 is not None:
            split_ratio_str = ""
            if config:
                split_ratio_str = config.get("PROVIDER_B_DUAL_ENTRY_SPLIT", "")
            if not split_ratio_str:
                split_ratio_str = os.getenv("PROVIDER_B_DUAL_ENTRY_SPLIT", "75/25")
                
            try:
                p1_str, p2_str = split_ratio_str.strip().split("/")
                w1 = float(p1_str) / 100.0
                w2 = float(p2_str) / 100.0
            except Exception:
                w1, w2 = 0.75, 0.25

            e1 = entry_price
            e2 = float(price2)

            if use_MILESTONE and sl_price:
                acc = mt5.account_info()
                bal = float(acc.balance) if acc and getattr(acc, "balance", None) else 100.0
                mm = self.calculate_MILESTONE_risk(bal, starting_milestone=starting_milestone)
                risk_cash = mm["total_risk"] * risk_mult
                lot1 = self._calculate_lot_from_money(symbol, e1, sl_price, risk_cash * w1, fallback_lot=round(fixed_lot * w1, 2))
                lot2 = self._calculate_lot_from_money(symbol, e2, sl_price, risk_cash * w2, fallback_lot=round(fixed_lot * w2, 2))
                logging.info(
                    f"[{channel_name or 'AutoTrader'}] Dual entry MILESTONE ({split_ratio_str}): E1 lot={lot1}, E2 lot={lot2} "
                    f"(Balance ${bal:.2f}, Milestone ${mm['milestone']}, Base ${mm['base_risk']} + Boost ${mm['boost']} [Tier {mm['tier']}], Total Risk ${risk_cash:.2f})"
                )
            elif use_risk and sl_price:
                lot1 = self._calculate_lot_size(symbol, e1, sl_price, risk_pct * w1, fixed_lot * w1)
                lot2 = self._calculate_lot_size(symbol, e2, sl_price, risk_pct * w2, fixed_lot * w2)
                logging.info(f"[{channel_name or 'AutoTrader'}] Dual entry risk mode ({split_ratio_str}): E1 lot={lot1} ({risk_pct * w1:.2f}%), E2 lot={lot2} ({risk_pct * w2:.2f}%)")
            else:
                lot1 = round(fixed_lot * w1, 2)
                lot2 = round(fixed_lot * w2, 2)
                info = mt5.symbol_info(symbol)
                vmin = getattr(info, "volume_min", 0.01) if info else 0.01
                if lot1 < vmin:
                    lot1 = vmin
                if lot2 < vmin:
                    lot2 = vmin
                logging.info(f"[{channel_name or 'AutoTrader'}] Dual entry fixed lot mode ({split_ratio_str}): E1 lot={lot1}, E2 lot={lot2}")

            request1 = {
                "action": mt5.TRADE_ACTION_PENDING,
                "symbol": symbol,
                "volume": float(lot1),
                "type": order_type,
                "price": e1,
                "deviation": 20,
                "magic": self.magic_number,
                "comment": f"Discord AutoTrader (E1 {int(w1*100)}%)",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": self._get_filling_mode(symbol),
            }
            if "sl" in signal:
                request1["sl"] = float(signal["sl"])
            if "tp" in signal:
                request1["tp"] = float(signal["tp"])

            request2 = {
                "action": mt5.TRADE_ACTION_PENDING,
                "symbol": symbol,
                "volume": float(lot2),
                "type": order_type,
                "price": e2,
                "deviation": 20,
                "magic": self.magic_number,
                "comment": f"Discord AutoTrader (E2 {int(w2*100)}%)",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": self._get_filling_mode(symbol),
            }
            if "sl" in signal:
                request2["sl"] = float(signal["sl"])
            if "tp" in signal:
                request2["tp"] = float(signal["tp"])

            res1 = mt5.order_send(request1)
            res2 = mt5.order_send(request2)

            sl_info = f" SL: {signal['sl']}" if "sl" in signal else ""
            tp_info = f" TP: {signal['tp']}" if "tp" in signal else ""
            risk_info = f" ({risk_mult}R Risk)" if risk_mult != 1.0 else ""
            
            placed = []
            errors = []
            if res1 and res1.retcode == mt5.TRADE_RETCODE_DONE:
                placed.append(f"Ticket #{res1.order} @ {e1} ({lot1} lots)")
                if channel_key and hasattr(res1, "order") and res1.order:
                    self.ticket_channel[res1.order] = channel_key
                log_trade_event("OPEN", symbol, action, e1, lot1, res1.order, 0.0, f"Entry 1 ({int(w1*100)}%){sl_info}{tp_info}{risk_info}".strip(), provider=getattr(self, "current_channel", "System"))
            else:
                err1 = res1.comment if res1 else mt5.last_error()
                errors.append(f"E1 failed: {err1}")
                
            if res2 and res2.retcode == mt5.TRADE_RETCODE_DONE:
                placed.append(f"Ticket #{res2.order} @ {e2} ({lot2} lots)")
                if channel_key and hasattr(res2, "order") and res2.order:
                    self.ticket_channel[res2.order] = channel_key
                log_trade_event("OPEN", symbol, action, e2, lot2, res2.order, 0.0, f"Entry 2 ({int(w2*100)}%){sl_info}{tp_info}{risk_info}".strip(), provider=getattr(self, "current_channel", "System"))
            else:
                err2 = res2.comment if res2 else mt5.last_error()
                errors.append(f"E2 failed: {err2}")
                
            if not placed and errors:
                return f"{auto_close_note}Dual limit orders failed for {symbol}: {', '.join(errors)}"
            elif errors:
                return f"{auto_close_note}Placed {len(placed)} LIMIT order for {symbol}: {', '.join(placed)}{sl_info}{tp_info}{risk_info}, but errors occurred: {', '.join(errors)}"
            else:
                return f"{auto_close_note}Placed 2 LIMIT orders for {symbol} ({split_ratio_str} split): {', '.join(placed)}{sl_info}{tp_info}{risk_info}"

        mm_info = ""
        if use_MILESTONE and sl_price:
            acc = mt5.account_info()
            bal = float(acc.balance) if acc and getattr(acc, "balance", None) else 100.0
            mm = self.calculate_MILESTONE_risk(bal, starting_milestone=starting_milestone)
            risk_cash = mm["total_risk"] * risk_mult
            lot_size = self._calculate_lot_from_money(symbol, entry_price, sl_price, risk_cash, fallback_lot=fixed_lot)
            mm_info = f" (Milestone MM: ${risk_cash:.2f})"
            logging.info(
                f"[{channel_name or 'AutoTrader'}] MILESTONE active: Balance ${bal:.2f} | Milestone ${mm['milestone']} | "
                f"Base ${mm['base_risk']} + Boost ${mm['boost']} (Tier {mm['tier']}) = ${mm['total_risk']:.2f} ({mm['effective_risk_pct']}%) | "
                f"Risk Cash ${risk_cash:.2f} -> Lot {lot_size}"
            )
        elif use_risk and sl_price:
            lot_size = self._calculate_lot_size(symbol, entry_price, sl_price, risk_pct, fixed_lot)
            logging.info(f"[{channel_name or 'AutoTrader'}] Risk mode active: lot size {lot_size} ({risk_pct}% risk with SL {sl_price})")
        elif (use_risk or use_MILESTONE) and not sl_price:
            lot_size = fixed_lot
            mode_desc = "MILESTONE" if use_MILESTONE else f"{risk_pct}% risk"
            logging.warning(f"[{channel_name or 'AutoTrader'}] {mode_desc} mode is enabled, but signal has no SL. Defaulted to fixed lot {lot_size}")
        else:
            lot_size = fixed_lot
            logging.info(f"[{channel_name or 'AutoTrader'}] Fixed lot mode: lot size {lot_size}")

        # Price proximity check for market orders
        threshold = self._get_pip_threshold(symbol)
        if order_type_str == "MARKET" and threshold is not None and "price" in signal:
            signal_cmp = signal["price"]
            pip_size = self._get_pip_size(symbol)
            max_deviation = threshold * pip_size
            deviation = abs(entry_price - signal_cmp)
            if deviation > max_deviation:
                return (f"Trade rejected: live price {entry_price} deviates {deviation/pip_size:.1f} pips "
                        f"from CMP {signal_cmp} (max allowed: {threshold} pips)")
        
        action_flag = mt5.TRADE_ACTION_PENDING if order_type_str != "MARKET" else mt5.TRADE_ACTION_DEAL
        
        request = {
            "action": action_flag,
            "symbol": symbol,
            "volume": float(lot_size),
            "type": order_type,
            "price": entry_price,
            "deviation": 20,
            "magic": self.magic_number,
            "comment": "Discord AutoTrader" if risk_mult == 1.0 else f"Discord AutoTrader ({risk_mult}R)",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": self._get_filling_mode(symbol),
        }
        
        if "sl" in signal:
            request["sl"] = float(signal["sl"])
        if "tp" in signal:
            request["tp"] = float(signal["tp"])
            
        result = mt5.order_send(request)
        if result is None:
            return f"Trade failed: order_send returned None (Error: {mt5.last_error()})"
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            return f"Trade failed: {result.comment} (Code: {result.retcode})"
            
        sl_info = f" SL: {signal['sl']}" if "sl" in signal else ""
        tp_info = f" TP: {signal['tp']}" if "tp" in signal else ""
        risk_info = f" ({risk_mult}R Risk)" if risk_mult != 1.0 else ""
        if channel_key and hasattr(result, "order") and result.order:
            self.ticket_channel[result.order] = channel_key

        log_trade_event("OPEN", symbol, action, entry_price, lot_size, result.order, 0.0, f"{sl_info}{tp_info}{risk_info}{mm_info}".strip(), provider=getattr(self, "current_channel", "System"))
        return f"{auto_close_note}Trade opened: {action} {symbol} at {entry_price}{sl_info}{tp_info}{risk_info}{mm_info} (Ticket: {result.order})"
        
    def _cancel_pending(self, signal: Dict[str, Any]) -> str:
        symbol = signal["symbol"]
        raw_symbol = signal.get("raw_symbol", "")
        orders = mt5.orders_get(symbol=symbol)
        if not orders and raw_symbol and raw_symbol != symbol:
            orders = mt5.orders_get(symbol=raw_symbol)
        if not orders:
            all_orders = mt5.orders_get()
            if all_orders:
                orders = [o for o in all_orders if o.symbol == symbol or (raw_symbol and o.symbol == raw_symbol)]
        if not orders:
            return f"Error: No pending orders found for {symbol}"
            
        cancelled = 0
        for order in orders:
            order_magic = getattr(order, "magic", None)
            if self.is_ticket_managed(order.ticket, order_magic):
                request = {
                    "action": mt5.TRADE_ACTION_REMOVE,
                    "order": order.ticket
                }
                res = mt5.order_send(request)
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    cancelled += 1
                    self.unadopt_ticket(order.ticket)
                    log_trade_event("CLOSE", order.symbol, "CANCEL", order.price_open, order.volume_initial, order.ticket, 0.0, "Pending order cancelled", provider=getattr(self, "current_channel", "System"))
                    
        if cancelled > 0:
            return f"Cancelled {cancelled} pending order(s) for {symbol}"
        return f"Error: No pending orders matching managed tickets for {symbol}"

    def _find_all_positions(self, symbol: str, raw_symbol: str = "") -> List[Any]:
        candidates = []
        positions = mt5.positions_get(symbol=symbol)
        if positions:
            candidates.extend(positions)
        if raw_symbol and raw_symbol != symbol:
            raw_pos = mt5.positions_get(symbol=raw_symbol)
            if raw_pos:
                candidates.extend(raw_pos)
        if not candidates:
            all_positions = mt5.positions_get()
            if all_positions:
                candidates.extend(all_positions)
                
        # Deduplicate and filter by magic number or adopted ticket and symbol matching
        matched = {}
        target_syms = {s for s in [symbol, raw_symbol] if s}
        for pos in candidates:
            pos_ticket = getattr(pos, "ticket", 0)
            pos_magic = getattr(pos, "magic", None)
            if self.is_ticket_managed(pos_ticket, pos_magic):
                pos_sym = getattr(pos, "symbol", "")
                if pos_sym in target_syms or any(pos_sym.startswith(ts) for ts in target_syms):
                    matched[pos.ticket] = pos
                    
        # Fallback: if no match by symbol name, check open positions for managed tickets
        if not matched:
            managed_positions = [
                p for p in candidates
                if self.is_ticket_managed(getattr(p, "ticket", 0), getattr(p, "magic", None))
            ]
            if len(managed_positions) == 1:
                logging.info(f"Symbol '{symbol}' did not match directly, falling back to single managed open position #{managed_positions[0].ticket} ({managed_positions[0].symbol})")
                matched[managed_positions[0].ticket] = managed_positions[0]
            elif len(managed_positions) > 1:
                unique_syms = {getattr(p, "symbol", "") for p in managed_positions}
                if len(unique_syms) == 1:
                    logging.info(f"Symbol '{symbol}' did not match directly, falling back to {len(managed_positions)} managed open positions of {list(unique_syms)[0]}")
                    for p in managed_positions:
                        matched[p.ticket] = p
                        
        return list(matched.values())

    def _find_position(self, symbol: str, raw_symbol: str = ""):
        all_pos = self._find_all_positions(symbol, raw_symbol)
        return all_pos[0] if all_pos else None
        
    def _update_sl(self, signal: Dict[str, Any]) -> str:
        symbol = signal["symbol"]
        raw_symbol = signal.get("raw_symbol", "")
        new_sl = signal["sl"]
        is_breakeven = signal.get("breakeven", False)
        
        positions = self._find_all_positions(symbol, raw_symbol)
        if not positions:
            return f"Error: No position with magic number found for {symbol}"
            
        updated_tickets = []
        errors = []
        
        for target_pos in positions:
            sl_to_set = float(target_pos.price_open) if is_breakeven else float(new_sl)
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": target_pos.symbol,
                "position": target_pos.ticket,
                "sl": sl_to_set,
                "tp": target_pos.tp,
                "magic": getattr(target_pos, "magic", self.magic_number)
            }
            
            result = mt5.order_send(request)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                err = result.comment if result else mt5.last_error()
                errors.append(f"#{target_pos.ticket}: {err}")
            else:
                updated_tickets.append(target_pos.ticket)
                note = f"SL moved to Breakeven ({sl_to_set})" if is_breakeven else f"SL moved to {sl_to_set}"
                log_trade_event("UPDATE_SL", target_pos.symbol, "MODIFY", sl_to_set, target_pos.volume, target_pos.ticket, target_pos.profit, note, provider=getattr(self, "current_channel", "System"))
                
        if not updated_tickets and errors:
            return f"SL Update failed for {symbol}: {', '.join(errors)}"
        elif errors:
            desc = f"Breakeven ({positions[0].price_open})" if is_breakeven else str(new_sl)
            return f"SL Updated to {desc} for {symbol} tickets {updated_tickets}, but errors on: {', '.join(errors)}"
        else:
            desc = f"Breakeven ({positions[0].price_open})" if is_breakeven else str(new_sl)
            return f"SL Updated for {symbol} to {desc} (ticket(s): {updated_tickets})"

    def _update_tp(self, signal: Dict[str, Any]) -> str:
        symbol = signal["symbol"]
        raw_symbol = signal.get("raw_symbol", "")
        new_tp = signal.get("tp", 0.0)
        
        positions = self._find_all_positions(symbol, raw_symbol)
        if not positions:
            return f"Error: No position with magic number found for {symbol}"
        
        updated_tickets = []
        errors = []
        
        for target_pos in positions:
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": target_pos.symbol,
                "position": target_pos.ticket,
                "sl": target_pos.sl,
                "tp": new_tp,
                "magic": getattr(target_pos, "magic", self.magic_number)
            }
            
            result = mt5.order_send(request)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                err = result.comment if result else mt5.last_error()
                errors.append(f"#{target_pos.ticket}: {err}")
            else:
                updated_tickets.append(target_pos.ticket)
                if new_tp == 0.0:
                    log_trade_event("UPDATE_TP", target_pos.symbol, "MODIFY", 0.0, target_pos.volume, target_pos.ticket, target_pos.profit, "TP removed (OPEN)", provider=getattr(self, "current_channel", "System"))
                else:
                    log_trade_event("UPDATE_TP", target_pos.symbol, "MODIFY", new_tp, target_pos.volume, target_pos.ticket, target_pos.profit, f"TP moved to {new_tp}", provider=getattr(self, "current_channel", "System"))
                
        if not updated_tickets and errors:
            return f"TP Update failed for {symbol}: {', '.join(errors)}"
        action_name = "removed (set to open)" if new_tp == 0.0 else f"moved to {new_tp}"
        return f"TP {action_name} for {symbol} (ticket(s): {updated_tickets})"
            
    def _close_position(self, signal: Dict[str, Any]) -> str:
        symbol = signal["symbol"]
        raw_symbol = signal.get("raw_symbol", "")
        positions = self._find_all_positions(symbol, raw_symbol)
        if not positions:
            return f"Error: No position with magic number found for {symbol}"
            
        closed_tickets = []
        errors = []
        result_note = signal.get("result", "")
        note_text = f"Closed via Discord signal ({result_note})" if result_note else "Closed via Discord signal"
        provider_price = f" (Signal price: {signal['price']})" if "price" in signal else ""
        
        for target_pos in positions:
            pos_symbol = target_pos.symbol
            tick = mt5.symbol_info_tick(pos_symbol)
            if not tick:
                errors.append(f"#{target_pos.ticket}: Could not retrieve tick data for {pos_symbol}")
                continue
                
            order_type = mt5.ORDER_TYPE_SELL if target_pos.type == 0 else mt5.ORDER_TYPE_BUY
            price = tick.bid if target_pos.type == 0 else tick.ask
            last_price = price
            
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": pos_symbol,
                "position": target_pos.ticket,
                "volume": float(target_pos.volume),
                "type": order_type,
                "price": price,
                "deviation": 20,
                "magic": getattr(target_pos, "magic", self.magic_number),
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": self._get_filling_mode(pos_symbol),
            }
            
            result = mt5.order_send(request)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                err = result.comment if result else mt5.last_error()
                errors.append(f"#{target_pos.ticket}: {err}")
            else:
                closed_tickets.append(target_pos.ticket)
                self.unadopt_ticket(target_pos.ticket)
                log_trade_event("CLOSE", pos_symbol, "CLOSE", price, target_pos.volume, target_pos.ticket, target_pos.profit, note_text, provider=getattr(self, "current_channel", "System"))
                
        price_str = f" at {last_price}" if 'last_price' in locals() else ""
        if not closed_tickets and errors:
            return f"Close failed for {symbol}: {', '.join(errors)}"
        elif errors:
            return f"Position closed: {symbol}{price_str} (ticket(s): {closed_tickets}){provider_price}, but errors occurred: {', '.join(errors)}"
        else:
            return f"Position closed: {symbol}{price_str} (ticket(s): {closed_tickets}){provider_price}"

    def get_active_breakeven_triggers(self) -> List[Dict[str, Any]]:
        """Return list of active pending breakeven price triggers."""
        return list(getattr(self, "active_breakeven_triggers", []))

    def _move_positions_to_breakeven(self, positions: list, trigger_price: float, is_immediate: bool = False) -> str:
        """Modifies Stop Loss to entry price (price_open) for the given positions."""
        updated_tickets = []
        errors = []
        for target_pos in positions:
            be_price = float(target_pos.price_open)
            request = {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": target_pos.symbol,
                "position": target_pos.ticket,
                "sl": be_price,
                "tp": target_pos.tp,
                "magic": self.magic_number
            }
            result = mt5.order_send(request)
            if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
                err = result.comment if result else mt5.last_error()
                errors.append(f"#{target_pos.ticket}: {err}")
            else:
                updated_tickets.append(target_pos.ticket)
                note = f"SL moved to Breakeven ({be_price}) - trigger {trigger_price} hit" if not is_immediate else f"SL moved to Breakeven ({be_price}) - CMP already at {trigger_price}"
                log_trade_event("UPDATE_SL", target_pos.symbol, "MODIFY", be_price, target_pos.volume, target_pos.ticket, target_pos.profit, note, provider=getattr(self, "current_channel", "System"))
                
        if not updated_tickets and errors:
            return f"Failed to move SL to Breakeven: {', '.join(errors)}"
        elif errors:
            return f"SL moved to Breakeven @ {positions[0].price_open} for ticket(s) {updated_tickets}, but errors occurred: {', '.join(errors)}"
        else:
            return f"✅ Breakeven Trigger Executed: SL moved to Breakeven @ {positions[0].price_open} for ticket(s) {updated_tickets}"

    def _handle_breakeven_trigger(self, signal: Dict[str, Any], config: Dict[str, str] = None) -> str:
        """
        Handles 'Breakeven set at <trigger_price>'.
        If current market price has already hit trigger_price, immediately moves SL to breakeven.
        Otherwise, arms a background price watcher that triggers when price reaches trigger_price.
        """
        import time
        symbol = signal["symbol"]
        raw_symbol = signal.get("raw_symbol", "")
        trigger_price = float(signal.get("trigger_price", signal.get("price", 0.0)))
        
        positions = self._find_all_positions(symbol, raw_symbol)
        if not positions:
            return f"Breakeven Watcher: No active position found for {symbol} to set trigger at {trigger_price}"
            
        pos = positions[0]
        is_buy = (pos.type == 0)
        
        # Check current market price
        tick = mt5.symbol_info_tick(symbol)
        if not tick and raw_symbol:
            tick = mt5.symbol_info_tick(raw_symbol)
            
        cmp = (tick.bid if is_buy else tick.ask) if tick else None
        
        # Check if condition is already satisfied:
        # For BUY: target price reached when CMP >= trigger_price
        # For SELL: target price reached when CMP <= trigger_price
        already_hit = False
        if cmp is not None:
            if is_buy and cmp >= trigger_price:
                already_hit = True
            elif not is_buy and cmp <= trigger_price:
                already_hit = True
                
        if already_hit:
            logging.info(f"Breakeven trigger {trigger_price} already reached by CMP ({cmp}). Executing immediately...")
            return self._move_positions_to_breakeven(positions, trigger_price, is_immediate=True)
            
        # Arm trigger for background monitoring
        trigger_id = f"{symbol}_{trigger_price}_{int(time.time())}"
        trigger_data = {
            "id": trigger_id,
            "symbol": symbol,
            "raw_symbol": raw_symbol,
            "trigger_price": trigger_price,
            "channel": getattr(self, "current_channel", ""),
            "is_buy": is_buy,
            "tickets": [p.ticket for p in positions],
            "be_price": pos.price_open,
            "config": config or {},
            "armed_at": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        
        if not hasattr(self, "active_breakeven_triggers"):
            self.active_breakeven_triggers = []
            
        # Remove any existing trigger for the exact same symbol and price
        self.active_breakeven_triggers = [
            t for t in self.active_breakeven_triggers
            if not (t["symbol"] == symbol and t["trigger_price"] == trigger_price)
        ]
        self.active_breakeven_triggers.append(trigger_data)
        self._start_breakeven_watcher()
        
        dir_str = "rises to" if is_buy else "drops to"
        cmp_str = f" (Current CMP: {cmp})" if cmp is not None else ""
        return f"🎯 Breakeven Trigger Armed: When {symbol} {dir_str} {trigger_price}{cmp_str}, SL will automatically move to Breakeven @ {pos.price_open} (ticket(s): {[p.ticket for p in positions]})"

    def _start_breakeven_watcher(self):
        """Starts background daemon thread for watching active price triggers."""
        if getattr(self, "_watcher_thread", None) is not None and self._watcher_thread.is_alive():
            return
        self._watcher_running = True
        self._watcher_thread = threading.Thread(target=self._breakeven_watch_loop, daemon=True)
        self._watcher_thread.start()
        logging.info("Background Breakeven Trigger Watcher thread launched.")

    def _breakeven_watch_loop(self):
        """Background thread polling live ticks and triggering SL breakeven adjustments."""
        import time
        while getattr(self, "_watcher_running", False) and getattr(self, "active_breakeven_triggers", []):
            try:
                triggers_to_remove = []
                with MT5_LOCK:
                    for trigger in list(self.active_breakeven_triggers):
                        symbol = trigger["symbol"]
                        channel = trigger.get("channel", "")
                        cfg = trigger.get("config", {})
                        
                        self._ensure_terminal_for_channel(channel, cfg)
                        
                        active_positions = self._find_all_positions(symbol, trigger.get("raw_symbol", ""))
                        target_tickets = set(trigger.get("tickets", []))
                        matching_positions = [p for p in active_positions if p.ticket in target_tickets]
                        
                        if not matching_positions:
                            logging.info(f"Breakeven trigger for {symbol} @ {trigger['trigger_price']} disarmed (positions already closed).")
                            triggers_to_remove.append(trigger["id"])
                            continue
                            
                        tick = mt5.symbol_info_tick(symbol)
                        if not tick:
                            continue
                            
                        is_buy = trigger["is_buy"]
                        current_price = tick.bid if is_buy else tick.ask
                        trigger_price = trigger["trigger_price"]
                        
                        hit = (current_price >= trigger_price) if is_buy else (current_price <= trigger_price)
                        if hit:
                            logging.info(f"🎯 Breakeven target hit! {symbol} reached {current_price} (Target: {trigger_price}). Modifying SL...")
                            exec_res = self._move_positions_to_breakeven(matching_positions, trigger_price, is_immediate=False)
                            logging.info(exec_res)
                            
                            # Send Telegram notification if configured
                            tg_token = os.getenv("TG_TOKEN") or cfg.get("TG_TOKEN")
                            tg_chat_id = os.getenv("TG_CHAT_ID") or cfg.get("TG_CHAT_ID")
                            if tg_token and tg_chat_id:
                                try:
                                    from src.notifier import send_telegram_message
                                    be_price = matching_positions[0].price_open
                                    chan_str = f" [#{channel}]" if channel else ""
                                    tg_msg = f"🎯 AutoTrader{chan_str}\n{symbol} hit Breakeven target {trigger_price}!\n✅ SL moved to Breakeven @ {be_price} for ticket(s) {[p.ticket for p in matching_positions]}"
                                    send_telegram_message(tg_token, tg_chat_id, tg_msg)
                                except Exception as tg_err:
                                    logging.warning(f"Failed to send Breakeven Telegram alert: {tg_err}")
                                    
                            triggers_to_remove.append(trigger["id"])
                            
                    if triggers_to_remove:
                        self.active_breakeven_triggers = [t for t in self.active_breakeven_triggers if t["id"] not in triggers_to_remove]
                        
            except Exception as e:
                logging.warning(f"Error in Breakeven watcher loop: {e}")
                
            time.sleep(1.0)
        self._watcher_running = False
        logging.info("Breakeven price watcher loop ended.")
