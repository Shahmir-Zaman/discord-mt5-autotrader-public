import csv
import os
from datetime import datetime, timedelta
from typing import Dict, Any

JOURNAL_FILE = 'trades_journal.csv'

def get_journal_file() -> str:
    global JOURNAL_FILE
    if JOURNAL_FILE != 'trades_journal.csv':
        return JOURNAL_FILE
    port = os.environ.get("AUTOTRADER_PORT", "").strip()
    if port and port != "8000":
        return f"trades_journal_{port}.csv"
    return JOURNAL_FILE

def log_trade_event(event_type: str, symbol: str, action: str, price: float, volume: float, ticket: int, profit: float = 0.0, notes: str = '', provider: str = 'System'):
    target_file = get_journal_file()
    file_exists = os.path.isfile(target_file)
    with open(target_file, mode='a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(['Timestamp', 'Provider', 'EventType', 'Symbol', 'Action', 'Price', 'Volume', 'Ticket', 'Profit', 'Notes'])
        writer.writerow([datetime.now().strftime('%Y-%m-%d %H:%M:%S'), provider, event_type, symbol, action, price, volume, ticket, profit, notes])

import threading
import time

_sync_lock = threading.Lock()
_last_sync_time = 0.0

def sync_journal_with_mt5(min_interval: float = 5.0, force: bool = False) -> int:
    """
    Scans trades_journal.csv for OPEN trades without a corresponding CLOSE.
    Checks MT5 to see if the position has closed (e.g. SL or TP hit, or closed externally).
    If closed in MT5, queries the exit deal from MT5 history and logs the CLOSE_MT5 event.
    Throttled by default to at most once every min_interval seconds.
    """
    global _last_sync_time
    now_ts = time.time()
    if not force and (now_ts - _last_sync_time) < min_interval:
        return 0

    target_file = get_journal_file()
    if not os.path.isfile(target_file):
        return 0

    with _sync_lock:
        now_ts = time.time()
        if not force and (now_ts - _last_sync_time) < min_interval:
            return 0
        _last_sync_time = now_ts

    try:
        import MetaTrader5 as mt5
    except ImportError:
        return 0

    if not mt5.initialize():
        return 0

    rows = []
    with open(target_file, mode='r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(r)

    opened_trades = {}
    closed_tickets = set()
    for r in rows:
        ticket = r.get("Ticket")
        evt = r.get("EventType")
        if not ticket:
            continue
        if evt == "OPEN":
            opened_trades[ticket] = r
        elif evt in ("CLOSE", "CLOSE_SIGNAL", "CLOSE_MANUAL", "CLOSE_MT5"):
            closed_tickets.add(ticket)

    unclosed_tickets = [t for t in opened_trades if t not in closed_tickets]
    if not unclosed_tickets:
        return 0

    provider_a_path = os.getenv("PROVIDER_A_MT5_PATH", r"C:\Program Files\MetaTrader 5\terminal64.exe")
    provider_b_path = os.getenv("PROVIDER_B_MT5_PATH", r"C:\Program Files\MetaTrader 5 - provider_b\terminal64.exe")

    synced_count = 0
    now_dt = datetime.now()
    from_date = now_dt - timedelta(days=14)

    for ticket in unclosed_tickets:
        try:
            t_int = int(ticket)
        except (ValueError, TypeError):
            continue

        open_info = opened_trades[ticket]
        provider = open_info.get("Provider", "System")
        symbol = open_info.get("Symbol", "")
        
        is_provider_b = any(kw in provider.lower() for kw in ["provider_b", "provider_b", "fx"])
        primary_path = provider_b_path if is_provider_b else provider_a_path
        secondary_path = provider_a_path if is_provider_b else provider_b_path

        deal = None
        found_open = False
        is_canceled_order = False
        
        for path in [primary_path, secondary_path]:
            if not path or not os.path.exists(path):
                continue
            try:
                if not mt5.initialize(path=path):
                    continue
            except Exception:
                continue
                
            # 1. Check if still active in open positions
            positions = mt5.positions_get(ticket=t_int)
            if positions and len(positions) > 0:
                found_open = True
                break
                
            # 2. Check if still active in pending orders
            active_orders = mt5.orders_get(ticket=t_int)
            if active_orders and len(active_orders) > 0:
                found_open = True
                break

            # 3. Query deals for this specific position/order
            pos_deals = mt5.history_deals_get(position=t_int)
            if not pos_deals:
                pos_deals = mt5.history_deals_get(ticket=t_int)
            if pos_deals:
                matching_deals = [d for d in pos_deals if getattr(d, 'position_id', None) == t_int or getattr(d, 'order', None) == t_int]
                exits = [d for d in matching_deals if getattr(d, 'entry', -1) == 1]
                if exits:
                    deal = exits[-1]
                    break

            # 4. Check if it was a canceled pending order
            hist_orders = mt5.history_orders_get(ticket=t_int)
            if hist_orders:
                canceled = [o for o in hist_orders if getattr(o, 'ticket', None) == t_int and getattr(o, 'state', None) in (2, 3, 4)]
                if canceled:
                    is_canceled_order = True
                    break
                    
        if found_open:
            continue

        if deal:
            close_price = deal.price
            profit = round(deal.profit, 2)
            comment = deal.comment.strip() if deal.comment else "Closed in MT5"
            close_time_str = datetime.fromtimestamp(deal.time).strftime('%Y-%m-%d %H:%M:%S')
            notes = f"Closed via MT5 ({comment})"
            action = 'CLOSE'
            event_type = 'CLOSE_MT5'
        elif is_canceled_order:
            close_price = float(open_info.get("Price", 0))
            profit = 0.0
            close_time_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            notes = "Pending order cancelled in MT5"
            action = 'CANCEL'
            event_type = 'CLOSE_MT5'
        else:
            # Ticket not found in active positions, pending orders, history deals, or history orders.
            # Do NOT fabricate false close rows.
            continue

        with open(target_file, mode='a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow([close_time_str, provider, event_type, symbol, action, close_price, open_info.get("Volume", 0.01), ticket, profit, notes])
        
        synced_count += 1

    return synced_count
