import re
from enum import Enum
from typing import Dict, Any, Optional

class SignalType(Enum):
    ENTRY = "ENTRY"
    SL_UPDATE = "SL_UPDATE"
    TP_UPDATE = "TP_UPDATE"
    CLOSE_POSITION = "CLOSE_POSITION"
    CANCEL_PENDING = "CANCEL_PENDING"
    BREAKEVEN_TRIGGER = "BREAKEVEN_TRIGGER"
    UNKNOWN = "UNKNOWN"

# Words indicating conversational, conditional, or future-hypothetical analysis
CONDITIONAL_REGEX = re.compile(
    r'\b(?:if|might|could|may|would|candle|candles|4h|h4|daily|weekly|monthly|watch|watching|wait|waiting|idk|let\'?s\s+say|maybe|forecast|below|above|expect|expecting)\b',
    re.IGNORECASE
)

# Common words to exclude from being treated as trading symbols
NON_SYMBOL_WORDS = {
    'THIS', 'TRADE', 'TRADES', 'POSITION', 'POSITIONS', 'CANDLE', 'CANDLES',
    'MIGHT', 'COULD', 'BELOW', 'ABOVE', 'MARKET', 'DAILY', 'WEEKLY', 'SOME',
    'HALF', 'ALL', 'NOW', 'HERE', 'SOON', 'LOSS', 'PROFIT', 'PIPS', 'CMP',
    'ORDER', 'ORDERS', 'ENTRY', 'APP', 'MANAGE', 'RISK', 'BUY', 'SELL', 'LONG', 'SHORT',
    # Days of week
    'MONDAY', 'TUESDAY', 'WEDNESDAY', 'THURSDAY', 'FRIDAY', 'SATURDAY', 'SUNDAY',
    'MON', 'TUE', 'WED', 'THU', 'FRI', 'SAT', 'SUN',
    # Months
    'JANUARY', 'FEBRUARY', 'MARCH', 'APRIL', 'MAY', 'JUNE',
    'JULY', 'AUGUST', 'SEPTEMBER', 'OCTOBER', 'NOVEMBER', 'DECEMBER',
    'JAN', 'FEB', 'MAR', 'APR', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC',
    # Discord names & words
    'ALERT', 'FOREX', 'EXAMPLE_ROLE', 'PROVIDER_C', 'PROVIDER_B', 'PROVIDER_A', 'PROVIDER_B',
    'EVERYONE', 'CHANNEL', 'CHALLENGE', 'HOUSE', 'TODAY', 'YESTERDAY',
    'UPDATE', 'MESSAGE', 'SIGNAL', 'INFO', 'PRICE', 'STOP', 'LIMIT',
    'TARGET', 'LEVEL', 'POINT', 'POINTS', 'BEFORE', 'AFTER', 'ALREADY',
    'AGAIN', 'CLOSE', 'CLOSED', 'CLOSING', 'OPEN', 'OPENED', 'OPENING',
    'BREAKEVEN', 'SHIFT', 'MOVE', 'MOVED', 'SET', 'TAKE', 'HIT', 'WAIT',
    'RESULT', 'FREE', 'TARGETS', 'PROVIDER_D', 'PROVIDER_D_ALIAS',
    # Common English / Chat / Urdu stop words to ignore as symbols
    'FOR', 'THE', 'AND', 'BUT', 'NOT', 'WITH', 'FROM', 'INTO', 'THAT', 'THEN',
    'WHAT', 'WHEN', 'WHERE', 'WHO', 'HOW', 'ANY', 'ARE', 'CAN', 'HAD', 'HAS',
    'HAVE', 'PUT', 'YOUR', 'KARLO', 'ENJOY', 'RIDE', 'AROUND', 'QUICK', 'STAGE',
    'JOIN', 'BHAI', 'TARDE', 'GOOD', 'KARDO', 'KHARDO', 'KHAIR', 'MEH', 'RISKY', 'ACTIVE',
    'SETUO', 'KINDLY', 'ONLY', 'BOIS', 'CUZ', 'MIN', 'TAP', 'THA', 'FAST', 'GUYS',
    'EXIT', 'LO', 'BE', 'SAFE', 'SIDE', 'LEVELS', 'MARKED'
}

COMMON_SYMBOLS = [
    'XAUUSD', 'GOLD', 'XAU', 'XAGUSD', 'SILVER',
    'EURUSD', 'GBPUSD', 'USDJPY', 'USDCAD', 'USDCHF', 'AUDUSD', 'NZDUSD',
    'EURJPY', 'GBPJPY', 'EURGBP', 'AUDJPY', 'EURAUD', 'EURCHF', 'GBPCHF',
    'US30', 'NAS100', 'SPX500', 'GER40', 'UK100', 'DJ30', 'US100', 'US500',
    'BTCUSD', 'ETHUSD', 'USOIL', 'UKOIL', 'WTI', 'BRENT'
]

def extract_symbol_from_text(text: str, default: str = "XAUUSD") -> str:
    """Intelligently identifies the trading symbol from message text, ignoring dates/headers/mentions."""
    # 1. Check if any known trading symbol appears in the text
    for sym in COMMON_SYMBOLS:
        if re.search(r'\b' + sym + r'\b', text, re.IGNORECASE):
            return sym.upper()
            
    # 2. Look for explicit uppercase symbol token (3-6 chars) that is not in NON_SYMBOL_WORDS
    tokens = re.findall(r'\b[A-Za-z0-9]{3,6}\b', text)
    for tok in tokens:
        upper = tok.upper()
        if upper not in NON_SYMBOL_WORDS and not upper.isdigit():
            if re.match(r'^[A-Z]{3,6}$', upper):
                return upper
                
    return default

def extract_risk_multiplier(text: str) -> float:
    """Extracts risk multiplier from signal text. Returns 0.5 for half risk, 1.0 otherwise."""
    if re.search(r'\b(?:half\s*[-]?risk(?:\s*\([\d.]+r\))?|1/2\s*risk|0\.5\s*r(?:isk)?|half\s+size|0\.5x\s*risk)\b', text, re.IGNORECASE):
        return 0.5
    r_match = re.search(r'\b(0\.\d+)\s*r(?:isk)?\b', text, re.IGNORECASE)
    if r_match:
        return float(r_match.group(1))
    return 1.0

def parse_signal(text: str) -> Dict[str, Any]:
    text = text.strip()
    if not text:
        return {"type": SignalType.UNKNOWN}
    
    # 1. Strip lines starting with mentions (@provider_b, @Forex Alert, etc.)
    lines = [line.strip() for line in text.splitlines() if not line.strip().startswith('@')]
    cleaned_text = '\n'.join(lines).strip()
    
    # 2. Strip Discord mentions (including hyphens and multi-word roles like @example_role-provider_c, @Forex Alert)
    cleaned_text = re.sub(r'<@&?\d+>', '', cleaned_text)
    cleaned_text = re.sub(r'@[\w-]+(?:\s+Alert)?', '', cleaned_text, flags=re.IGNORECASE)
    
    # 3. Strip bracketed timestamps: e.g. "[ 12:21 ]", "[12:21:05 PM]"
    cleaned_text = re.sub(r'\[\s*\d{1,2}:\d{2}(?::\d{2})?\s*(?:AM|PM)?\s*\]', '', cleaned_text, flags=re.IGNORECASE)
    
    # 4. Strip date headers: e.g. "Monday, 21 September 2026 at 12:21" or "Wednesday, 26 August 2026 at 14:18"
    cleaned_text = re.sub(
        r'\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)[,\s]+(?:\d{1,2}\s+[A-Za-z]+\s+\d{4})?(?:\s+at\s+\d{1,2}:\d{2}(?::\d{2})?)?\b',
        '',
        cleaned_text,
        flags=re.IGNORECASE
    )
    cleaned_text = re.sub(r'\b(?:Today|Yesterday)\s+at\s+\d{1,2}:\d{2}(?::\d{2})?\b', '', cleaned_text, flags=re.IGNORECASE)
    
    # 5. Strip "Author APP HH:MM" headers: e.g. "example_role - provider_c APP 12:19"
    cleaned_text = re.sub(r'\b[\w\s-]+APP\s+\d{1,2}:\d{2}\b', '', cleaned_text, flags=re.IGNORECASE)
    cleaned_text = cleaned_text.strip()
    
    # 1. Match ENTRY: BUY/SELL SYMBOL CMP [PRICE] or BUY/SELL CMP [SYMBOL] [PRICE]
    entry_match = re.search(r'\b(BUY|SELL)(?:\s+([A-Za-z0-9]{3,6}))?\s+CMP(?:\s+([A-Za-z0-9]{3,6}))?', cleaned_text, re.IGNORECASE)
    pending_match = re.search(r'(?:PLACE\s+)?([A-Z0-9]+)\s+(BUY|SELL)\s+(LIMIT|STOP)', cleaned_text, re.IGNORECASE)
    
    if entry_match or pending_match:
        if entry_match:
            action = entry_match.group(1).upper()
            sym_candidate = entry_match.group(2) or entry_match.group(3)
            if sym_candidate and not sym_candidate.isdigit() and sym_candidate.upper() not in NON_SYMBOL_WORDS:
                raw_sym = sym_candidate.upper()
                symbol = "XAUUSD" if raw_sym in ["GOLD", "XAU"] else raw_sym
            else:
                symbol = extract_symbol_from_text(cleaned_text, default="XAUUSD")
            order_type = "MARKET"
        else:
            symbol = pending_match.group(1).upper()
            action = pending_match.group(2).upper()
            order_type = pending_match.group(3).upper()
            
        signal = {
            "type": SignalType.ENTRY,
            "action": action,
            "symbol": symbol,
            "order_type": order_type
        }
        
        # Try to extract the entry price
        if order_type == "MARKET":
            price_match = re.search(r'(?:CMP\s*[:\s]*|Entry\s*[:\s]*)([\d.]+)', cleaned_text, re.IGNORECASE)
            if price_match:
                signal["price"] = float(price_match.group(1))
        else:
            price_match = re.search(r'Entry\s*:\s*([\d.]+)(?:\s*(?:[|/,–—\-]|to|and)\s*([\d.]+))?', cleaned_text, re.IGNORECASE)
            if price_match:
                signal["price"] = float(price_match.group(1))
                if price_match.group(2):
                    signal["price2"] = float(price_match.group(2))
        
        # Check for initial SL in entry (e.g. "SL : 4520.0" or "SL 4142.7" or "SL: 4161")
        sl_match = re.search(r'SL\s*[:\s]*([\d.]+)', cleaned_text, re.IGNORECASE)
        if sl_match:
            signal["sl"] = float(sl_match.group(1))
            
        # Check for initial TP in entry (e.g. "TP : 4535.0" or "TP 1 : 4535.0")
        tp_match = re.search(r'TP(?:\s*1)?\s*[:\s]*([\d.]+)', cleaned_text, re.IGNORECASE)
        if tp_match:
            signal["tp"] = float(tp_match.group(1))
            
        # Check for half risk / risk multiplier
        mult = extract_risk_multiplier(cleaned_text)
        signal["risk_multiplier"] = mult
        signal["half_risk"] = (mult == 0.5)
            
        return signal

    # 1b. Match provider_b/Discord format with standalone Entry / SL (e.g. "Entry : 4430.4\nSL : 4424.9 (-56 pips)")
    standalone_entry_match = re.search(r'Entry\s*:\s*([\d.]+)(?:\s*(?:[|/,–—\-]|to|and)\s*([\d.]+))?', cleaned_text, re.IGNORECASE)
    if standalone_entry_match:
        entry_price = float(standalone_entry_match.group(1))
        entry_price2 = float(standalone_entry_match.group(2)) if standalone_entry_match.group(2) else None
        sl_match = re.search(r'SL\s*:\s*([\d.]+)', cleaned_text, re.IGNORECASE)
        sl_price = float(sl_match.group(1)) if sl_match else None
        
        action_match = re.search(r'\b(BUY|SELL)\b', cleaned_text, re.IGNORECASE)
        if action_match:
            action = action_match.group(1).upper()
        elif sl_price is not None:
            action = "BUY" if entry_price > sl_price else "SELL"
        else:
            action = "BUY"
            
        sym_match = re.search(r'\b(XAUUSD|EURUSD|GBPUSD|USDJPY|US30|NAS100|[A-Z]{3,6})\b', cleaned_text, re.IGNORECASE)
        symbol = "XAUUSD"
        if sym_match and sym_match.group(1).upper() not in ["CMP", "LIMIT", "STOP", "ENTRY", "RISK", "PIPS", "MANAGE", "APP"]:
            symbol = sym_match.group(1).upper()
            
        signal = {
            "type": SignalType.ENTRY,
            "action": action,
            "symbol": symbol,
            "order_type": "LIMIT",
            "price": entry_price
        }
        if entry_price2 is not None:
            signal["price2"] = entry_price2
        if sl_price is not None:
            signal["sl"] = sl_price
            
        tp_match = re.search(r'TP(?:\s*1)?\s*:\s*([\d.]+)', cleaned_text, re.IGNORECASE)
        if tp_match:
            signal["tp"] = float(tp_match.group(1))
            
        # Check for half risk / risk multiplier
        mult = extract_risk_multiplier(cleaned_text)
        signal["risk_multiplier"] = mult
        signal["half_risk"] = (mult == 0.5)
            
        return signal

    # 2. Match Explicit provider_d / Chat close command: e.g. "Close in 10 pips loss", "Exit from the tarde in 10 pips loss", "close kardo", "Book your tarde here"
    provider_d_close = re.search(
        r'\b(?:Close\s+in\s+\d+\s+pips\s+loss|Exit\s+(?:from\s+the\s+tarde\s+in|at)\s+\d+\s+pips\s+loss|close\s+kardo|Book\s+your\s+(?:tarde|trade|profits)\s+here)\b',
        cleaned_text,
        re.IGNORECASE
    )
    if provider_d_close:
        sym = extract_symbol_from_text(cleaned_text, default="XAUUSD")
        return {
            "type": SignalType.CLOSE_POSITION,
            "symbol": sym,
            "result": provider_d_close.group(0)
        }

    # 2b. Match Take TP / Close Position (strictly reject if conditional/analysis keywords exist)
    if not CONDITIONAL_REGEX.search(cleaned_text):
        # 2a. provider_a format: "SYMBOL Trade Closed ..." or "SYMBOL Take TP [1] here ..."
        provider_a_close = re.search(r'\b([A-Z0-9]{3,6})\s+(?:Trade\s+Closed|Take\s+TP(?:\s*\d+)?\s+here)\b', cleaned_text, re.IGNORECASE)
        if provider_a_close and provider_a_close.group(1).upper() not in NON_SYMBOL_WORDS:
            sig = {
                "type": SignalType.CLOSE_POSITION,
                "symbol": provider_a_close.group(1).upper()
            }
            price_match = re.search(r'\(([\d.]+)\)', cleaned_text)
            if price_match:
                sig["price"] = float(price_match.group(1))
            res_match = re.search(r'Result\s*:\s*([^\n]+)', cleaned_text, re.IGNORECASE)
            if res_match:
                sig["result"] = res_match.group(1).strip()
            return sig
            
        # 2b. Explicit close command on its own line: e.g. "Close XAUUSD", "Close XAUUSD now", "Close half XAUUSD"
        cmd_close = re.search(r'(?:^|\n)\s*Close(?:\s+(?:all|half|trade|position))?\s+([A-Z0-9]{3,6})(?:\s+(?:now|here|CMP))?\s*(?:$|\n)', cleaned_text, re.IGNORECASE)
        if cmd_close and cmd_close.group(1).upper() not in NON_SYMBOL_WORDS:
            return {
                "type": SignalType.CLOSE_POSITION,
                "symbol": cmd_close.group(1).upper()
            }
            
        # 2c. Simple "SYMBOL Closed" on its own line: e.g. "XAUUSD Closed"
        simple_closed = re.search(r'(?:^|\n)\s*([A-Z0-9]{3,6})\s+Closed(?:\s*(?:now|here|at\s+[\d.]+))?\s*(?:$|\n)', cleaned_text, re.IGNORECASE)
        if simple_closed and simple_closed.group(1).upper() not in NON_SYMBOL_WORDS:
            return {
                "type": SignalType.CLOSE_POSITION,
                "symbol": simple_closed.group(1).upper()
            }

    # 3. Match Price-Triggered Breakeven Watcher:
    # e.g.: "XAUUSD Breakeven set at 4390.0", "XAUUSD Set Breakeven at 4390.0", "Breakeven set at 4390.0"
    if not CONDITIONAL_REGEX.search(cleaned_text):
        be_trigger_match = re.search(
            r'(?:(?:Set\s+)?Breakeven\s+(?:set\s+)?at|Breakeven\s+target(?:\s*at)?|Move\s+SL\s+to\s+Breakeven\s+at)\s+([\d.]+)',
            cleaned_text,
            re.IGNORECASE
        )
        if be_trigger_match:
            trigger_price = float(be_trigger_match.group(1))
            sym = extract_symbol_from_text(cleaned_text, default="XAUUSD")
            return {
                "type": SignalType.BREAKEVEN_TRIGGER,
                "symbol": sym,
                "trigger_price": trigger_price,
                "price": trigger_price
            }

    # 4. Match SL Update (including Shift SL, Move SL, Set SL, Move SL to Breakeven):
    # e.g.: "XAUUSD Shift SL to 4351.0 (-224 pips)", "XAUUSD Set SL to 4611.0", "XAUUSD SL moved to Breakeven (4426.2)"
    if not CONDITIONAL_REGEX.search(cleaned_text):
        # 4a. Immediate breakeven commands without explicit numeric price
        # e.g.: "SL BE fast", "SL at BE fast", "Put your SL BE fast", "Move your SL to BE", "SL BE karlo", "Exit at BE", "BE fast"
        be_direct_match = re.search(
            r'\b(?:(?:Put\s+your\s+|Move\s+(?:your\s+)?)?SL\s*(?:at\s+|to\s+)?BE\b|SL\s+BE\b|Move\s+(?:your\s+)?SL\s+to\s+entry\b|Guys\s+Exit\s+lo\s+BE\b|Exit\s+at\s+BE\b|BE\s+fast\b)',
            cleaned_text,
            re.IGNORECASE
        )
        if be_direct_match:
            sym = extract_symbol_from_text(cleaned_text, default="XAUUSD")
            return {
                "type": SignalType.SL_UPDATE,
                "symbol": sym,
                "sl": 0.0,
                "breakeven": True
            }

        sl_match = re.search(
            r'(?:(?:Set|Move|Shift)\s+SL|SL\s+(?:set|moved|move|shift|shifted|to)?)\s*(?:to\s+)?(?:Breakeven\s*\(?)?([\d.]+)',
            cleaned_text,
            re.IGNORECASE
        )
        if sl_match:
            sym = extract_symbol_from_text(cleaned_text, default="XAUUSD")
            return {
                "type": SignalType.SL_UPDATE,
                "symbol": sym,
                "sl": float(sl_match.group(1))
            }
        
    # 5. Match TP Update / Open: SYMBOL \n TP OPEN or SYMBOL (Set/Move/Shift TP | TP set/moved/to) [to] PRICE
    # e.g.: "XAUUSD Shift TP to 4444.0", "XAUUSD Set TP to 4363.0 (+89 pips)", "XAUUSD Move TP to 4550.0"
    if not CONDITIONAL_REGEX.search(cleaned_text):
        tp_match = re.search(
            r'(?:(?:Set|Move|Shift)\s+TP|TP\s+(?:set|moved|move|shift|shifted|to)?)\s*(?:to\s+)?([\d.]+)',
            cleaned_text,
            re.IGNORECASE
        )
        if tp_match:
            sym = extract_symbol_from_text(cleaned_text, default="XAUUSD")
            return {
                "type": SignalType.TP_UPDATE,
                "symbol": sym,
                "tp": float(tp_match.group(1))
            }
            
        tp_open_match = re.search(r'TP\s+OPEN', cleaned_text, re.IGNORECASE)
        if tp_open_match:
            sym = extract_symbol_from_text(cleaned_text, default="XAUUSD")
            return {
                "type": SignalType.TP_UPDATE,
                "symbol": sym,
                "tp": 0.0
            }
        
    # 6. Match Cancel Pending Order: SYMBOL Pending Order Removed
    if not CONDITIONAL_REGEX.search(cleaned_text):
        cancel_match = re.search(r'Pending\s+Order\s+(?:Removed|Cancelled)', cleaned_text, re.IGNORECASE)
        if cancel_match:
            sym = extract_symbol_from_text(cleaned_text, default="XAUUSD")
            return {
                "type": SignalType.CANCEL_PENDING,
                "symbol": sym
            }
        
    return {"type": SignalType.UNKNOWN}
