import os
import sys
import time
import logging
import threading
from typing import Optional, Dict, Deque
from collections import deque
import requests
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Thread-safe rate limiter and deduplication state
_STATE_LOCK = threading.Lock()
_RECENT_ERRORS: Dict[str, float] = {}   # error_key -> last_sent_time
_SENT_TIMESTAMPS: Deque[float] = deque() # rolling window of timestamps in last 60s
_MAX_ERRORS_PER_MINUTE = 10
_DEBOUNCE_SECONDS = 45.0

def _get_tg_credentials(token: Optional[str] = None, chat_id: Optional[str] = None):
    t = (token or "").strip()
    c = (chat_id or "").strip()
    if not t or not c:
        try:
            load_dotenv(override=False)
        except Exception:
            pass
        t = t or os.getenv("TG_TOKEN", "").strip()
        c = c or os.getenv("TG_CHAT_ID", "").strip()
    return t, c

def send_telegram_message(token: str, chat_id: str, message: str) -> bool:
    """
    Sends a text message to Telegram via Bot API.
    Truncates message to 4096 characters if required by Telegram limits.
    """
    t, c = _get_tg_credentials(token, chat_id)
    if not t or not c:
        logger.warning("Cannot send Telegram message: TG_TOKEN or TG_CHAT_ID is missing.")
        return False

    # Telegram message text limit is 4096 characters
    if len(message) > 4090:
        message = message[:4080] + "\n...[truncated]"

    url = f"https://api.telegram.org/bot{t}/sendMessage"
    payload = {"chat_id": c, "text": message}
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        # Use warning to avoid triggering recursive TelegramLoggingHandler loops
        logger.warning(f"Failed to send Telegram message: {e}")
        return False

def send_telegram_error(
    error_message: str,
    context: Optional[str] = None,
    token: Optional[str] = None,
    chat_id: Optional[str] = None,
    force: bool = False
) -> bool:
    """
    Sends an urgent error alert to Telegram with rate-limiting and anti-spam debouncing.
    """
    if not error_message:
        return False

    t, c = _get_tg_credentials(token, chat_id)
    if not t or not c:
        return False

    now = time.time()
    ctx_str = f" [{context}]" if context else ""
    dedup_key = f"{context or ''}:{error_message.strip()[:120]}"

    with _STATE_LOCK:
        # 1. Debounce check: identical error within debounce window
        if not force:
            last_time = _RECENT_ERRORS.get(dedup_key)
            if last_time and (now - last_time) < _DEBOUNCE_SECONDS:
                return False

        # 2. Rate limit check: max per minute
        while _SENT_TIMESTAMPS and now - _SENT_TIMESTAMPS[0] > 60.0:
            _SENT_TIMESTAMPS.popleft()

        if len(_SENT_TIMESTAMPS) >= _MAX_ERRORS_PER_MINUTE and not force:
            return False

        _RECENT_ERRORS[dedup_key] = now
        # Clean up stale recent errors
        if len(_RECENT_ERRORS) > 200:
            for k, v in list(_RECENT_ERRORS.items()):
                if now - v > 300:
                    _RECENT_ERRORS.pop(k, None)

        _SENT_TIMESTAMPS.append(now)

    timestamp_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))
    formatted = (
        f"🚨 [AutoTrader ERROR Alert{ctx_str}]\n"
        f"⏰ {timestamp_str}\n\n"
        f"{error_message.strip()}"
    )

    return send_telegram_message(t, c, formatted)

class TelegramLoggingHandler(logging.Handler):
    """
    Logging handler that forwards ERROR and CRITICAL logs to Telegram.
    Guards against recursion and ignores errors from network/telegram libraries.
    """
    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None):
        super().__init__(level=logging.ERROR)
        self.token = token
        self.chat_id = chat_id

    def emit(self, record: logging.LogRecord):
        # Prevent outbound calls during pytest runs unless specifically enabled
        if os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("TEST_TELEGRAM_HANDLER"):
            return

        # Prevent recursive loops from HTTP/Telegram logging
        if record.name in ("src.notifier", "urllib3", "requests", "urllib3.connectionpool"):
            return
        if "Failed to send Telegram message" in str(record.getMessage()):
            return

        try:
            msg = self.format(record)
            ctx = f"{record.name}:{record.funcName}"
            send_telegram_error(msg, context=ctx, token=self.token, chat_id=self.chat_id)
        except Exception:
            self.handleError(record)

def setup_telegram_error_logging(logger_obj: Optional[logging.Logger] = None):
    """
    Attaches the TelegramLoggingHandler to the specified logger or root logger.
    Idempotent: will not attach multiple handlers.
    """
    target = logger_obj or logging.getLogger()
    for h in target.handlers:
        if isinstance(h, TelegramLoggingHandler):
            return h

    handler = TelegramLoggingHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    target.addHandler(handler)
    return handler
