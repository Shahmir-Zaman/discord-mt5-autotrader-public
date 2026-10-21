import requests
import json
import logging
import time
from typing import Callable, Any, Dict

def normalize_ntfy_url(topic_url: str) -> str:
    """Ensures topic URL includes scheme and host. Defaults naked topic names to https://ntfy.sh/<topic>."""
    url = (topic_url or "").strip()
    if not url:
        return ""
    if not url.startswith("http://") and not url.startswith("https://"):
        return f"https://ntfy.sh/{url}"
    return url

def listen_ntfy(topic_url: str, callback: Callable[[Dict[str, Any]], None], max_retries: int = 0):
    """Listens to ntfy stream and calls callback on new message.
    
    Automatically reconnects with exponential backoff on disconnection.
    Set max_retries=0 for infinite retries (default for production).
    """
    topic_url = normalize_ntfy_url(topic_url)
    retry_count = 0
    base_delay = 5  # seconds
    max_delay = 300  # 5 minutes cap
    
    while True:
        try:
            logging.info(f"Connecting to ntfy topic: {topic_url}")
            response = requests.get(f"{topic_url}/json", stream=True, timeout=(15, 300))
            retry_count = 0  # Reset on successful connection
            
            for line in response.iter_lines():
                if line:
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if data.get("event") == "message":
                        title = data.get("title", "")
                        logging.info(f"Received ntfy notification (title: '{title}'). Triggering extraction.")
                        try:
                            callback(data)
                        except TypeError:
                            callback()
                        
        except KeyboardInterrupt:
            logging.info("Listener stopped by user.")
            raise
        except Exception as e:
            retry_count += 1
            if max_retries > 0 and retry_count > max_retries:
                logging.error(f"Max retries ({max_retries}) exceeded. Stopping listener.")
                raise
            delay = min(base_delay * (2 ** (retry_count - 1)), max_delay)
            logging.warning(f"Ntfy listener error: {e}. Reconnecting in {delay}s (attempt {retry_count})...")
            time.sleep(delay)
