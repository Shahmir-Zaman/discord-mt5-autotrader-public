from unittest.mock import patch, MagicMock
import os
import logging
import requests
from src.notifier import (
    send_telegram_message,
    send_telegram_error,
    TelegramLoggingHandler,
    setup_telegram_error_logging
)

def test_send_telegram_message_success():
    with patch("requests.post") as mock_post:
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        res = send_telegram_message(token="dummy_token", chat_id="12345", message="Test message")

        assert res is True
        mock_post.assert_called_once_with(
            "https://api.telegram.org/botdummy_token/sendMessage",
            json={"chat_id": "12345", "text": "Test message"},
            timeout=10,
        )

def test_send_telegram_message_failure():
    with patch("requests.post") as mock_post:
        mock_post.side_effect = requests.RequestException("Network error")

        res = send_telegram_message(token="dummy_token", chat_id="12345", message="Test message")

        assert res is False

def test_send_telegram_error_success():
    with patch("requests.post") as mock_post:
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        res = send_telegram_error(
            "Symbol XAUUSD rejected: Market closed",
            context="MT5 Execution",
            token="err_token",
            chat_id="err_chat",
            force=True
        )

        assert res is True
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert "boterr_token/sendMessage" in args[0]
        assert kwargs["json"]["chat_id"] == "err_chat"
        payload_text = kwargs["json"]["text"]
        assert "🚨 [AutoTrader ERROR Alert [MT5 Execution]]" in payload_text
        assert "Symbol XAUUSD rejected: Market closed" in payload_text

def test_send_telegram_error_debounce():
    with patch("requests.post") as mock_post:
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        err_text = "Repeated database timeout error"
        res1 = send_telegram_error(err_text, context="Database", token="t", chat_id="c")
        assert res1 is True
        assert mock_post.call_count == 1

        # Second call with exact same error should be debounced
        res2 = send_telegram_error(err_text, context="Database", token="t", chat_id="c")
        assert res2 is False
        assert mock_post.call_count == 1  # Not called again

        # Force call bypasses debounce
        res3 = send_telegram_error(err_text, context="Database", token="t", chat_id="c", force=True)
        assert res3 is True
        assert mock_post.call_count == 2

def test_send_telegram_error_missing_creds():
    with patch.dict(os.environ, {"TG_TOKEN": "", "TG_CHAT_ID": ""}):
        res = send_telegram_error("Some error", token="", chat_id="")
        assert res is False

def test_telegram_logging_handler():
    with patch("requests.post") as mock_post:
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        handler = TelegramLoggingHandler(token="log_tok", chat_id="log_chat")
        test_logger = logging.getLogger("test_error_subsystem")
        test_logger.propagate = False
        test_logger.addHandler(handler)
        test_logger.setLevel(logging.ERROR)

        try:
            with patch.dict(os.environ, {"TEST_TELEGRAM_HANDLER": "1"}):
                test_logger.error("Something went critically wrong in worker!")

            assert mock_post.call_count == 1
            payload_text = mock_post.call_args[1]["json"]["text"]
            assert "Something went critically wrong in worker!" in payload_text
        finally:
            test_logger.removeHandler(handler)
