# MOneyTransfer

Telegram bot (aiogram v3) for ZMW ↔ RUB quotes using Google Sheet rate, with fee brackets and a menu UI.
- Local run (polling): `python bot.py`
- Webhook server (Render): `python bot_webhook.py`
- Env vars needed: `TELEGRAM_BOT_TOKEN`, `GOOGLE_SHEET_URL` (or `GOOGLE_SHEET_CSV_URL`)
