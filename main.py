import os
import sys
import logging
from dotenv import load_dotenv
from src.listener import listen_ntfy
from src.extractor import extract_discord_message, extract_recent_discord_messages
from src.parser import parse_signal, SignalType
from src.executor import MT5Executor
from src.notifier import send_telegram_message, send_telegram_error, setup_telegram_error_logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

from typing import Optional, Dict, Any

from collections import OrderedDict

# Track processed message IDs per channel to prevent duplicate trades (FIFO bounded to last 100)
_processed_ids = OrderedDict()

# Channel routing: maps target channel name to allowed author/channel search keywords
CHANNEL_ROUTING = {
    "channel-a": ["provider_a", "channel-a", "example_provider_a", "house", "channel-a"],
    "channel-b": ["provider_b", "channel-b", "! provider_b ali", "provider_b"],
    "channel-c": ["provider_c", "channel-c", "provider_c_fx"],
    "provider_d": ["provider_d", "🦅", "provider_d_alias", "xau provider_d", "123456789012345678"],
}

def identify_channel(data: Any) -> Optional[str]:
    """Identifies which target channel an ntfy notification belongs to, or None if unapproved."""
    if not isinstance(data, dict):
        return None
    title = data.get("title", "").lower()
    message = data.get("message", "").lower()
    combined = f"{title} {message}"
    
    for channel_target, keywords in CHANNEL_ROUTING.items():
        for kw in keywords:
            if kw in combined:
                return channel_target
    return None

def is_message_valid(msg_id: str, ntfy_time: Optional[float] = None, max_age: float = 300.0) -> bool:
    """Validates message age using Discord's Snowflake ID timestamp."""
    import time
    if not msg_id or not msg_id.startswith("chat-messages-"):
        return True
    try:
        parts = msg_id.split("-")
        snowflake = int(parts[-1])
        msg_time = ((snowflake >> 22) + 1420070400000) / 1000.0
        current_time = time.time()
        
        # Max Age Check: Ignore if message is older than max_age
        age = current_time - msg_time
        if age > max_age:
            logging.warning(f"Ignored message {msg_id}: Too old ({age/60:.1f} mins ago, limit {max_age/60:.1f} mins).")
            return False
            
        return True
    except Exception as e:
        logging.error(f"Failed to validate message timestamp: {e}")
        return True

def on_notification(data: Any, executor: MT5Executor, tg_token: str, tg_chat_id: str, lot_size: float):
    try:
        target_channel = identify_channel(data)
        
        if not target_channel:
            title = data.get("title", "") if isinstance(data, dict) else str(data)
            logging.info(f"Ignoring notification from unapproved sender/channel (title: '{title}').")
            return
            
        logging.info(f"Notification matched allowed channel: #{target_channel}. Extracting message...")
        
        # Retry extraction to handle transient CDP timeouts
        recent_msgs = []
        detected_channel = ""
        for extract_attempt in range(2):
            recent_msgs, detected_channel = extract_recent_discord_messages(channel_keyword=target_channel, limit=10)
            if recent_msgs:
                break
            if extract_attempt == 0:
                logging.warning(f"No text extracted for #{target_channel} on attempt 1, retrying in 1.5s...")
                import time as _time
                _time.sleep(1.5)
        
        if not recent_msgs:
            msg_id, text, detected_channel = extract_discord_message(channel_keyword=target_channel)
            if text:
                recent_msgs = [{"id": msg_id, "text": text}]
        
        if not recent_msgs:
            logging.warning(f"No text extracted for #{target_channel} after retries.")
            return
        
        ntfy_time = float(data.get("time", 0)) if isinstance(data, dict) and "time" in data else None
        
        for msg_obj in recent_msgs:
            msg_id = msg_obj.get("id", "")
            text = msg_obj.get("text", "")
            if not msg_id or not text:
                continue
                
            dedup_key = f"{target_channel}:{msg_id}"
            if dedup_key in _processed_ids:
                continue
                
            signal = parse_signal(text)
            if signal["type"] == SignalType.UNKNOWN:
                _processed_ids[dedup_key] = True
                if len(_processed_ids) > 100:
                    _processed_ids.popitem(last=False)
                logging.info(f"[#{target_channel}] Signal parsed as UNKNOWN. Ignoring.")
                continue
                
            is_close_or_cancel = signal["type"] in (SignalType.CLOSE_POSITION, SignalType.CANCEL_PENDING)
            max_age = 600.0 if is_close_or_cancel else 300.0
            
            if not is_message_valid(msg_id, ntfy_time=ntfy_time, max_age=max_age):
                _processed_ids[dedup_key] = True
                if len(_processed_ids) > 100:
                    _processed_ids.popitem(last=False)
                continue
                
            _processed_ids[dedup_key] = True
            if len(_processed_ids) > 100:
                _processed_ids.popitem(last=False)
                
            logging.info(f"[#{target_channel}] Extracted: {text}")
            logging.info(f"[#{target_channel}] Parsed Signal: {signal}")
            try:
                result_msg = executor.execute_signal(signal, channel=target_channel, config=os.environ)
            except Exception as e:
                result_msg = f"Execution Exception: {e}"
                logging.error(f"[#{target_channel}] {result_msg}", exc_info=True)
            
            logging.info(f"[#{target_channel}] {result_msg}")
            is_error = isinstance(result_msg, str) and any(kw in result_msg.lower() for kw in [
                "error:", "trade failed", "close failed", "update failed", "rejected", "cannot", "security halt", "exception"
            ])
            if tg_token and tg_chat_id:
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
        logging.error(f"Error handling notification: {e}", exc_info=True)
        send_telegram_error(f"Error handling notification: {e}", context="Notification Handler", token=tg_token, chat_id=tg_chat_id)

def main():
    # Load .env file (if it exists) before reading env vars
    load_dotenv()
    setup_telegram_error_logging()
    
    # Validate required env vars
    ntfy_topic = os.environ.get("NTFY_TOPIC", "").strip()
    if ntfy_topic and not ntfy_topic.startswith("http://") and not ntfy_topic.startswith("https://"):
        ntfy_topic = f"https://ntfy.sh/{ntfy_topic}"
    tg_token = os.environ.get("TG_TOKEN")
    tg_chat_id = os.environ.get("TG_CHAT_ID")
    lot_size = float(os.environ.get("LOT_SIZE", "0.01"))
    
    missing = []
    if not ntfy_topic:
        missing.append("NTFY_TOPIC")
    if not tg_token:
        missing.append("TG_TOKEN")
    if not tg_chat_id:
        missing.append("TG_CHAT_ID")
    
    if missing:
        logging.error(f"Missing required environment variables: {', '.join(missing)}")
        logging.error("Set them before running: NTFY_TOPIC, TG_TOKEN, TG_CHAT_ID")
        sys.exit(1)
    
    executor = MT5Executor()

    logging.info("Starting Discord MT5 AutoTrader (Multi-Channel: provider_a & provider_b Example)...")
    try:
        listen_ntfy(ntfy_topic, lambda data: on_notification(data, executor, tg_token, tg_chat_id, lot_size))
    except KeyboardInterrupt:
        logging.info("Shutting down...")
    finally:
        executor.shutdown()

if __name__ == "__main__":
    main()
