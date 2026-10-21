# Discord MT5 AutoTrader — public edition

A Python application for parsing Discord trading signals, routing them to locally configured MetaTrader 5 terminals, and displaying status in a FastAPI dashboard. Includes a Chrome extension for forwarding messages from Discord Web to the local application.

## Public snapshot

This repository starts with fresh history. It excludes private credentials, instance configuration, saved Discord messages, trading history, internal notes, original commit metadata, compiled Python files, and unreviewed images. Provider identities, account numbers, notification endpoints and Discord channel IDs have been replaced with neutral examples. Configuration keys have also been generalized; private instance configuration is not compatible without adaptation. No license has been added.

## Setup

1. Use Windows with a compatible Python installation and MetaTrader 5 terminal. Sign into your own trading account locally.
2. Create and activate a Python virtual environment. Install dependencies with `python -m pip install -r requirements.txt`. The CDP extraction path also supports the optional `websockets` package; Playwright is its fallback.
3. Copy `.env.example` to `.env` and enter only your own local configuration. Leave optional Telegram, ntfy and monitoring settings empty until needed.
4. Configure terminal paths, expected account logins, and channel routing in `main.py`, `src/extractor.py`, `web/service.py` and `extension/content.js`. The supplied channel and account identifiers are synthetic examples; replace them locally.
5. Start the local dashboard with `python app.py` and open the localhost URL printed by the application. Review configuration before starting the bot.

The application uses an already authenticated MT5 terminal session. It does not include account passwords. Start with a demo account and review all routing and order settings before allowing execution.

## Chrome extension

Open Chrome extension management, enable Developer mode, choose Load unpacked and select `extension/`. Configure its localhost target ports and enable forwarding only for channels you are authorized to access. The source icons are omitted; Chrome uses its default extension icon. CDP extraction is an alternative and requires your own locally configured Chrome debugging session.

## Local use

The supplied dashboard has no authentication, can return locally configured Telegram credentials to the extension, and its launcher listens on all network interfaces. Run it only on a trusted, isolated machine/network; do not expose the dashboard or Chrome debugging port to the internet. Keep `.env`, generated `config_*.json`, logs and trade journals out of Git.

## Validation

Existing tests are included with generalized fixtures. Run `python -m pytest` in a suitable Windows Python environment. This snapshot has not been validated against a live broker, Discord account or Telegram service.
