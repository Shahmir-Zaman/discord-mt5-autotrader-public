from unittest.mock import patch, MagicMock
from src.extractor import extract_discord_message, extract_recent_discord_messages

def test_extract_discord_message_success_by_keyword():
    mock_browser = MagicMock()
    mock_context = MagicMock()
    
    mock_page_provider_a = MagicMock()
    mock_page_provider_a.url = 'https://discord.com/channels/123/456'
    mock_page_provider_a.title.return_value = 'Discord | #channel-a | Server'
    mock_page_provider_a.evaluate.return_value = {'id': 'msg-1', 'text': 'BUY XAUUSD CMP 4625.9', 'channel': 'channel-a'}
    
    mock_page_provider_b = MagicMock()
    mock_page_provider_b.url = 'https://discord.com/channels/123/789'
    mock_page_provider_b.title.return_value = 'Discord | #channel-b | Server'
    mock_page_provider_b.evaluate.return_value = {'id': 'msg-2', 'text': 'BUY XAUUSD CMP 4526.5', 'channel': 'channel-b'}
    
    mock_context.pages = [mock_page_provider_a, mock_page_provider_b]
    mock_browser.contexts = [mock_context]
    
    with patch('src.extractor._extract_via_direct_cdp', return_value=None), \
         patch('src.extractor.sync_playwright') as mock_sp:
        mock_p = MagicMock()
        mock_p.chromium.connect_over_cdp.return_value = mock_browser
        mock_sp.return_value.__enter__.return_value = mock_p
        
        msg_id, text, channel = extract_discord_message(channel_keyword='channel-b')
        
        assert msg_id == 'msg-2'
        assert '4526.5' in text
        assert channel == 'channel-b'

def test_extract_discord_message_no_tabs():
    mock_browser = MagicMock()
    mock_context = MagicMock()
    mock_context.pages = []
    mock_browser.contexts = [mock_context]
    
    with patch('src.extractor._extract_via_direct_cdp', return_value=None), \
         patch('src.extractor.sync_playwright') as mock_sp:
        mock_p = MagicMock()
        mock_p.chromium.connect_over_cdp.return_value = mock_browser
        mock_sp.return_value.__enter__.return_value = mock_p
        
        msg_id, text, channel = extract_discord_message(channel_keyword='channel-a')
        assert msg_id == ''
        assert text == ''
        assert channel == ''

def test_extract_discord_message_unmatched_channel_returns_empty():
    """Requesting an unmatched channel should not fall back to other unrelated tabs."""
    mock_browser = MagicMock()
    mock_context = MagicMock()
    
    mock_page_provider_b = MagicMock()
    mock_page_provider_b.url = 'https://discord.com/channels/123/789'
    mock_page_provider_b.title.return_value = 'Discord | #channel-b | Server'
    mock_page_provider_b.evaluate.return_value = {'id': 'msg-2', 'text': 'BUY XAUUSD CMP 4526.5', 'channel': 'channel-b'}
    
    mock_context.pages = [mock_page_provider_b]
    mock_browser.contexts = [mock_context]
    
    with patch('src.extractor._extract_via_direct_cdp', return_value=None), \
         patch('src.extractor.sync_playwright') as mock_sp:
        mock_p = MagicMock()
        mock_p.chromium.connect_over_cdp.return_value = mock_browser
        mock_sp.return_value.__enter__.return_value = mock_p
        
        # We request 'channel-a' but only 'channel-b' is open
        msg_id, text, channel = extract_discord_message(channel_keyword='channel-a')
        assert msg_id == ''
        assert text == ''
        assert channel == ''

def test_extract_recent_discord_messages_multi():
    mock_browser = MagicMock()
    mock_context = MagicMock()
    
    mock_page = MagicMock()
    mock_page.url = 'https://discord.com/channels/123/456'
    mock_page.title.return_value = 'Discord | #channel-a | Server'
    mock_page.evaluate.return_value = {
        'channel': 'channel-a',
        'recent': [
            {'id': 'msg-1', 'text': 'Move SL to Breakeven'},
            {'id': 'msg-2', 'text': 'Trade Closed (+52 pips)'},
            {'id': 'msg-3', 'text': 'Mein DAY CLOSE KR RAH BUSSSS'}
        ]
    }
    
    mock_context.pages = [mock_page]
    mock_browser.contexts = [mock_context]
    
    with patch('src.extractor._extract_via_direct_cdp', return_value=None), \
         patch('src.extractor.sync_playwright') as mock_sp:
        mock_p = MagicMock()
        mock_p.chromium.connect_over_cdp.return_value = mock_browser
        mock_sp.return_value.__enter__.return_value = mock_p
        
        msgs, channel = extract_recent_discord_messages(channel_keyword='channel-a', limit=5)
        assert len(msgs) == 3
        assert msgs[0]['id'] == 'msg-1'
        assert msgs[1]['text'] == 'Trade Closed (+52 pips)'
        assert msgs[2]['text'] == 'Mein DAY CLOSE KR RAH BUSSSS'
        assert channel == 'channel-a'

def test_direct_cdp_extraction_fast():
    """Direct CDP returns messages directly without touching Playwright."""
    expected_msgs = [{"id": "direct-1", "text": "BUY XAUUSD CMP 4180.0"}]
    with patch('src.extractor._extract_via_direct_cdp', return_value=(expected_msgs, 'channel-a')) as mock_direct, \
         patch('src.extractor.sync_playwright') as mock_sp:
        msgs, ch = extract_recent_discord_messages(channel_keyword='channel-a')
        assert msgs == expected_msgs
        assert ch == 'channel-a'
        mock_direct.assert_called_once()
        mock_sp.assert_not_called()