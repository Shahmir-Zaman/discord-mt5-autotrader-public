from playwright.sync_api import sync_playwright
import logging
import asyncio
import json
import time
import requests
import concurrent.futures
from typing import Tuple, Optional, List, Dict

try:
    import websockets
except ImportError:
    websockets = None

CHANNEL_ALIASES = {
    "channel-a": ["channel-a", "house", "channel-a", "provider_a", "123456789012345678"],
    "channel-a": ["channel-a", "house", "channel-a", "provider_a", "123456789012345678"],
    "provider_a": ["channel-a", "house", "channel-a", "provider_a", "123456789012345678"],
    "channel-b": ["channel-b", "provider_b", "provider_b", "123456789012345678"],
    "provider_b": ["channel-b", "provider_b", "provider_b", "123456789012345678"],
    "channel-c": ["channel-c", "provider_c", "123456789012345678"],
    "provider_c": ["channel-c", "provider_c", "123456789012345678"],
    "provider_d": ["provider_d", "🦅", "provider_d_alias", "123456789012345678", "xau provider_d"],
    "🦅-provider_d": ["provider_d", "🦅", "provider_d_alias", "123456789012345678", "xau provider_d"],
    "provider_d_alias": ["provider_d", "🦅", "provider_d_alias", "123456789012345678", "xau provider_d"],
}

def _extract_via_direct_cdp(channel_keyword: Optional[str] = None, debug_port: int = 9222, limit: int = 10) -> Optional[Tuple[List[Dict[str, str]], str]]:
    """Ultra-fast direct CDP extraction using Chrome's native WebSocket debugger per tab (~80ms)."""
    if not websockets:
        return None
        
    try:
        r = requests.get(f"http://127.0.0.1:{debug_port}/json", timeout=0.8)
        if r.status_code != 200:
            return None
        tabs = r.json()
        if not isinstance(tabs, list):
            return None
            
        discord_tabs = [t for t in tabs if "discord.com/channels" in t.get("url", "")]
        if not discord_tabs:
            return None
            
        target_tab = None
        if channel_keyword:
            kw_clean = channel_keyword.lower().replace("#", "").strip()
            aliases = CHANNEL_ALIASES.get(kw_clean, [kw_clean])
            for t in discord_tabs:
                title = t.get("title", "").lower()
                url = t.get("url", "").lower()
                if any(a in title or a in url for a in aliases):
                    target_tab = t
                    break
        if not target_tab:
            if channel_keyword:
                return None
            target_tab = discord_tabs[0]
            
        ws_url = target_tab.get("webSocketDebuggerUrl")
        if not ws_url:
            return None
            
        async def _eval():
            async with websockets.connect(ws_url, open_timeout=1.5, close_timeout=1.0) as ws:
                js = f"""(() => {{
                    const jumpBar = document.querySelector('[class*="jumpToPresentBar"] button, [class*="jumpToPresentBar"], [class*="bar-"] button');
                    if (jumpBar) {{
                        try {{ jumpBar.click(); }} catch(e) {{}}
                    }}
                    const scroller = document.querySelector('[class*="messagesWrapper"] [class*="scroller-"], [data-list-id="chat-messages"]');
                    if (scroller && scroller.scrollHeight > (scroller.scrollTop + scroller.clientHeight + 20)) {{
                        scroller.scrollTop = scroller.scrollHeight;
                    }}
                    const messages = Array.from(document.querySelectorAll('li[id^="chat-messages-"]'));
                    const sliceCount = Math.max(1, {limit});
                    function getMsgText(m) {{
                        const contentEl = m.querySelector('[id^="message-content-"], [class*="messageContent"]');
                        return (contentEl && contentEl.innerText && contentEl.innerText.trim()) ? contentEl.innerText.trim() : m.innerText.trim();
                    }}
                    const recentSlice = messages.slice(-sliceCount).map(m => ({{id: m.id, text: getMsgText(m)}}));
                    const header = document.querySelector('h1, h2, h3, [class*="title-"], [class*="channelName-"]');
                    const channelName = header ? header.innerText.trim() : document.title;
                    return JSON.stringify({{
                        channel: channelName,
                        recent: recentSlice
                    }});
                }})()"""
                req = {
                    "id": 1,
                    "method": "Runtime.evaluate",
                    "params": {"expression": js, "returnByValue": True}
                }
                await ws.send(json.dumps(req))
                raw_res = await asyncio.wait_for(ws.recv(), timeout=2.0)
                data = json.loads(raw_res)
                eval_res = data.get("result", {}).get("result", {}).get("value")
                if eval_res:
                    parsed = json.loads(eval_res)
                    return (parsed.get("recent", []), parsed.get("channel", "discord"))
                return ([], "")

        def _run_async(coro):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop and loop.is_running():
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    return pool.submit(asyncio.run, coro).result(timeout=2.5)
            else:
                return asyncio.run(coro)

        return _run_async(_eval())
    except Exception as e:
        logging.warning(f"Direct CDP extraction failed: {e}")
        return None

def extract_recent_discord_messages(channel_keyword: Optional[str] = None, debug_port: int = 9222, limit: int = 10, max_retries: int = 3) -> Tuple[List[Dict[str, str]], str]:
    """Connects to Chrome via CDP and extracts recent Discord messages from target channel tab.
    
    Uses ultra-fast direct WebSocket CDP extraction (~80ms) with retries, then falls back to Playwright.
    
    Args:
        channel_keyword: Optional keyword to match the channel tab (e.g. 'channel-a', 'channel-a', 'channel-b').
        debug_port: Chrome DevTools Protocol port (default: 9222).
        limit: Max number of recent messages to return (default: 10).
        max_retries: Number of retry attempts for CDP extraction before giving up (default: 3).
        
    Returns:
        Tuple of (list_of_messages, channel_name). Each message is {'id': str, 'text': str}.
    """
    # 1. Primary fast path with retries: direct WebSocket CDP (~80ms, no node.exe process spawn)
    for attempt in range(max_retries):
        fast_result = _extract_via_direct_cdp(channel_keyword=channel_keyword, debug_port=debug_port, limit=limit)
        if fast_result is not None:
            return fast_result
        if attempt < max_retries - 1:
            backoff = 0.5 * (attempt + 1)  # 0.5s, 1.0s
            logging.info(f"CDP extraction attempt {attempt + 1}/{max_retries} failed for #{channel_keyword}, retrying in {backoff}s...")
            time.sleep(backoff)

    # 2. Fallback path: Playwright (with strict 3000ms timeout to prevent 180s hanging)
    cdp_url = f"http://127.0.0.1:{debug_port}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(cdp_url, timeout=3000)
            
            if not browser.contexts:
                logging.error("No browser contexts found.")
                return ([], "")
            
            context = browser.contexts[0]
            discord_pages = [page for page in context.pages if "discord.com/channels" in page.url]
            
            if not discord_pages:
                logging.error("No Discord tabs found in browser.")
                return ([], "")
            
            target_page = None
            detected_channel = channel_keyword or ""
            
            if channel_keyword:
                kw_lower = channel_keyword.lower().replace("#", "").strip()
                search_keywords = CHANNEL_ALIASES.get(kw_lower, [kw_lower])
                for page in discord_pages:
                    title_lower = page.title().lower()
                    url_lower = page.url.lower()
                    
                    # 1. Match against page title or URL
                    if any(k in title_lower or k in url_lower for k in search_keywords):
                        target_page = page
                        break
                    
                    # 2. Check DOM channel header
                    try:
                        channel_header = page.evaluate('''() => {
                            const header = document.querySelector('h1, h2, h3, [class*="title-"], [class*="channelName-"]');
                            return header ? header.innerText.toLowerCase() : '';
                        }''')
                        if any(k in channel_header for k in search_keywords):
                            target_page = page
                            break
                    except Exception:
                        pass
                        
            # If a specific channel_keyword was requested but not found, do NOT scrape an unrelated channel tab!
            if not target_page:
                if channel_keyword:
                    logging.warning(f"Could not find Discord tab specifically matching '{channel_keyword}'. Aborting extraction to prevent cross-channel misattribution.")
                    return ([], "")
                target_page = discord_pages[0]

            # Ensure the view is scrolled to the latest messages and dismiss any "Jump to Present" bar
            try:
                scrolled = target_page.evaluate('''() => {
                    let didScroll = false;
                    const jumpBar = document.querySelector('[class*="jumpToPresentBar"] button, [class*="jumpToPresentBar"], [class*="bar-"] button');
                    if (jumpBar) {
                        try { jumpBar.click(); didScroll = true; } catch(e) {}
                    }
                    const scroller = document.querySelector('[class*="messagesWrapper"] [class*="scroller-"], [data-list-id="chat-messages"]');
                    if (scroller && scroller.scrollHeight > (scroller.scrollTop + scroller.clientHeight + 20)) {
                        scroller.scrollTop = scroller.scrollHeight;
                        didScroll = true;
                    }
                    return didScroll;
                }''')
                if scrolled is True and hasattr(target_page, "wait_for_timeout"):
                    target_page.wait_for_timeout(250)
            except Exception:
                pass
                
            # Evaluate JS to get recent messages and channel title
            result = target_page.evaluate(f'''() => {{
                const messages = Array.from(document.querySelectorAll('li[id^="chat-messages-"]'));
                const header = document.querySelector('h1, h2, h3, [class*="title-"], [class*="channelName-"]');
                const channelName = header ? header.innerText.trim() : document.title;
                if (messages.length === 0) return {{id: "", text: "", channel: channelName, recent: []}};
                
                const sliceCount = Math.max(1, {limit});
                function getMsgText(m) {{
                    const contentEl = m.querySelector('[id^="message-content-"], [class*="messageContent"]');
                    return (contentEl && contentEl.innerText && contentEl.innerText.trim()) ? contentEl.innerText.trim() : m.innerText.trim();
                }}
                const recentSlice = messages.slice(-sliceCount).map(m => ({{id: m.id, text: getMsgText(m)}}));
                const lastMessage = messages[messages.length - 1];
                return {{
                    id: lastMessage.id,
                    text: getMsgText(lastMessage),
                    channel: channelName,
                    recent: recentSlice
                }};
            }}''')
            
            found_channel = detected_channel or (result.get("channel", "discord") if isinstance(result, dict) else "discord")
            recent_list = result.get("recent") if isinstance(result, dict) else None
            if recent_list is not None and isinstance(recent_list, list):
                return (recent_list, found_channel)
            
            # Fallback for mock environments returning only single id/text
            if isinstance(result, dict):
                found_id = result.get("id", "")
                found_text = result.get("text", "")
                if found_id or found_text:
                    return ([{"id": found_id, "text": found_text}], found_channel)
            return ([], found_channel)
            
    except Exception as e:
        logging.error(f"CDP Extraction failed: {e}")
        return ([], "")

def extract_discord_message(channel_keyword: Optional[str] = None, debug_port: int = 9222) -> Tuple[str, str, str]:
    """Connects to Chrome via CDP and extracts latest Discord message from target channel tab.
    
    Args:
        channel_keyword: Optional keyword to match the channel tab (e.g. 'channel-a', 'channel-a', 'channel-b').
        debug_port: Chrome DevTools Protocol port (default: 9222).
        
    Returns:
        Tuple of (message_id, message_text, channel_name). All empty strings on failure.
    """
    messages, channel = extract_recent_discord_messages(channel_keyword=channel_keyword, debug_port=debug_port, limit=1)
    if messages:
        last_msg = messages[-1]
        return (last_msg.get("id", ""), last_msg.get("text", ""), channel)
    return ("", "", channel)
