import json
from unittest.mock import patch, MagicMock
import requests
from src.listener import listen_ntfy, normalize_ntfy_url

def test_normalize_ntfy_url():
    assert normalize_ntfy_url("your-private-topic") == "https://ntfy.sh/your-private-topic"
    assert normalize_ntfy_url("https://ntfy.sh/your-private-topic") == "https://ntfy.sh/your-private-topic"
    assert normalize_ntfy_url("http://custom.domain.com/topic") == "http://custom.domain.com/topic"
    assert normalize_ntfy_url("") == ""
    assert normalize_ntfy_url(None) == ""

def test_listen_ntfy_normalizes_naked_topic():
    mock_callback = MagicMock()
    with patch("requests.get") as mock_get, patch("time.sleep"):
        mock_get.side_effect = requests.RequestException("Stop test")
        try:
            listen_ntfy("your-private-topic", mock_callback, max_retries=1)
        except Exception:
            pass
        assert mock_get.call_count >= 1
        assert mock_get.call_args_list[0][0][0] == "https://ntfy.sh/your-private-topic/json"

def test_listen_ntfy_triggers_callback_on_message():
    mock_callback = MagicMock()
    lines = [
        json.dumps({"event": "open"}).encode("utf-8"),
        b"",
        json.dumps({"event": "message", "message": "new alert"}).encode("utf-8"),
        json.dumps({"event": "keepalive"}).encode("utf-8"),
    ]

    with patch("requests.get") as mock_get, \
         patch("time.sleep"):
        mock_response = MagicMock()
        mock_response.iter_lines.return_value = lines
        # First call succeeds, then keep raising to break the loop
        exc = requests.RequestException("done")
        mock_get.side_effect = [mock_response, exc, exc, exc]

        try:
            listen_ntfy("https://ntfy.sh/my_topic", mock_callback, max_retries=2)
        except Exception:
            pass

        assert mock_callback.call_count == 1

def test_listen_ntfy_handles_exception():
    mock_callback = MagicMock()

    with patch("requests.get") as mock_get, \
         patch("time.sleep"):  # Don't actually wait during tests
        mock_get.side_effect = requests.RequestException("Connection failed")

        # Should raise after max_retries exceeded
        try:
            listen_ntfy("https://ntfy.sh/my_topic", mock_callback, max_retries=1)
        except requests.RequestException:
            pass

        assert mock_callback.call_count == 0
