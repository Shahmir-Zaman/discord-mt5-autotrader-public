import pytest
from unittest.mock import patch, MagicMock
import csv
import os
import tempfile
from src import journal

def test_sync_journal_ignores_active_pending_order(tmp_path, monkeypatch):
    journal_csv = tmp_path / 'trades_journal.csv'
    with open(journal_csv, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['Timestamp', 'Provider', 'EventType', 'Symbol', 'Action', 'Price', 'Volume', 'Ticket', 'Profit', 'Notes'])
        writer.writerow(['2026-09-15 17:21:45', 'channel-a', 'OPEN', 'XAUUSDm', 'BUY', 4010.0, 0.12, 200001, 0.0, 'SL: 4000.0'])

    monkeypatch.setattr(journal, 'JOURNAL_FILE', str(journal_csv))
    
    mock_mt5 = MagicMock()
    mock_mt5.initialize.return_value = True
    # Not in open positions
    mock_mt5.positions_get.return_value = ()
    # BUT in active pending orders!
    mock_order = MagicMock()
    mock_order.ticket = 200001
    mock_mt5.orders_get.return_value = (mock_order,)
    
    with patch.dict('sys.modules', {'MetaTrader5': mock_mt5}):
        synced = journal.sync_journal_with_mt5(force=True)
        assert synced == 0
        
    with open(journal_csv, 'r', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1  # No CLOSE_MT5 was written!

def test_sync_journal_correctly_matches_position_deal(tmp_path, monkeypatch):
    journal_csv = tmp_path / 'trades_journal.csv'
    with open(journal_csv, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(['Timestamp', 'Provider', 'EventType', 'Symbol', 'Action', 'Price', 'Volume', 'Ticket', 'Profit', 'Notes'])
        writer.writerow(['2026-09-15 18:27:56', 'channel-a', 'OPEN', 'XAUUSDm', 'SELL', 4283.52, 0.12, 200002, 0.0, 'SL: 4300.0'])

    monkeypatch.setattr(journal, 'JOURNAL_FILE', str(journal_csv))
    
    mock_mt5 = MagicMock()
    mock_mt5.initialize.return_value = True
    mock_mt5.positions_get.return_value = ()
    mock_mt5.orders_get.return_value = ()
    
    # An exit deal matching this position
    mock_deal = MagicMock()
    mock_deal.position_id = 200002
    mock_deal.order = 200003
    mock_deal.entry = 1
    mock_deal.price = 4283.816
    mock_deal.profit = -3.55
    mock_deal.comment = 'Discord AutoTrader'
    mock_deal.time = 1789485371
    
    mock_mt5.history_deals_get.return_value = (mock_deal,)
    
    with patch.dict('sys.modules', {'MetaTrader5': mock_mt5}):
        synced = journal.sync_journal_with_mt5(force=True)
        assert synced == 1
        
    with open(journal_csv, 'r', encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 2
    assert rows[1]['EventType'] == 'CLOSE_MT5'
    assert float(rows[1]['Profit']) == -3.55
    assert float(rows[1]['Price']) == 4283.816
    assert rows[1]['Ticket'] == '200002'