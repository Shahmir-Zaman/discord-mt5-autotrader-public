import pytest
import os
import src.journal

@pytest.fixture(autouse=True)
def isolate_env():
    """Ensure os.environ changes in one test do not leak to other tests."""
    old_env = dict(os.environ)
    os.environ["TARGET_TERMINAL"] = "auto"
    os.environ.pop("PROVIDER_A_SYMBOL_SUFFIX", None)
    os.environ.pop("PROVIDER_B_SYMBOL_SUFFIX", None)
    os.environ.pop("TERMINAL_OWNER_SYMBOL_SUFFIX", None)
    os.environ.pop("PROVIDER_D_SYMBOL_SUFFIX", None)
    os.environ.pop("SYMBOL_SUFFIX", None)
    yield
    os.environ.clear()
    os.environ.update(old_env)

@pytest.fixture(autouse=True)
def isolate_journal(tmp_path, monkeypatch):
    """Ensure all tests write to a temporary trades_journal.csv so real data is never modified."""
    dummy_journal = tmp_path / "trades_journal.csv"
    dummy_journal.write_text("Timestamp,Provider,EventType,Symbol,Action,Price,Volume,Ticket,Profit,Notes\n")
    monkeypatch.setattr(src.journal, "JOURNAL_FILE", str(dummy_journal))
