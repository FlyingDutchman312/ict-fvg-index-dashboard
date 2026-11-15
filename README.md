# Nasdaq & S&P 500 ICT + FVG Conservative Dashboard — V3

## Features
- Streamlit charts for NQ/ES futures, Nasdaq/S&P 500 indices, and QQQ/SPY ETFs, using 5-minute and 15-minute historical Yahoo Finance candles.
- **Conservative** ICT-inspired sweep → market structure shift → displacement + 3-candle FVG → EMA confirmation, 2:1 reference reward:risk.
- Scheduled **Telegram entry, stop-loss, take-profit/exit alerts** for NQ, ES, Nasdaq-100 (^NDX) and S&P 500 (^GSPC) on both timeframes (configurable; QQQ, SPY and Nasdaq Composite available). All signals are **hypothetical paper setups**; no orders are placed.
- Duplicate prevention and a persistent paper-position ledger saved to `data/alerts_state.json` by GitHub Actions.
- Dashboard shows recorded alerts/open hypothetical setups from its deployed Git checkout.

## Upload V3 to your existing GitHub repository
1. Unzip the package. Upload/replace root files `app.py`, `strategy.py`, `monitor.py`, `requirements.txt`, and `README.md` in your existing `ict-fvg-index-dashboard` repository.
2. Upload `data/alerts_state.json` into a folder named **data** in that repository. It is deliberately public/nonsecret.
3. Create the workflow file at **`.github/workflows/telegram-monitor.yml`** (not the repo root). GitHub's browser **Add file → Create new file** accepts the full nested path as the filename; paste the included YAML content, then commit.
4. Streamlit's GitHub integration should redeploy the app on commit. Your Streamlit main file remains `app.py`.

## Create Telegram bot
1. In Telegram, message the verified **@BotFather**, send `/newbot`, choose a bot name and username, and copy its API token. **Never paste the token in public GitHub files, Streamlit text fields, chat, or screenshots.**
2. Open your new bot's Telegram chat and send **/start**.
3. In a private browser tab, open `https://api.telegram.org/botYOUR_BOT_TOKEN/getUpdates` with `YOUR_BOT_TOKEN` replaced locally. Find the numeric value under `result` → `message` → `chat` → `id`. If the result is empty, send your bot another message and retry. Treat the URL/token as sensitive; close the tab when finished.
4. GitHub repository → **Settings → Secrets and variables → Actions → New repository secret**. Add secrets named `TELEGRAM_BOT_TOKEN` (your token) and `TELEGRAM_CHAT_ID` (numeric chat ID). Do not use Streamlit secrets for this workflow.

## Enable and test alerts
1. GitHub repository → **Actions** → **ICT FVG Telegram Paper Alerts** → **Run workflow** (default `main` branch). Watch the job logs for errors.
2. A manual run may send **no Telegram alert**, correctly, when no fresh conservative setup exists. No test trades/signals are fabricated.
3. To confirm Telegram delivery independently, run this one-time command **locally** (never put your token in GitHub files):

   `python telegram_test.py`

   It reads `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from your local environment. Alternatively use Telegram's HTTP `sendMessage` endpoint privately.
4. If the workflow reports permission errors on push, go to repository **Settings → Actions → General → Workflow permissions**, and ensure the repository permits write access. The workflow requests `contents: write`, but administrator restrictions may override it.
5. Scheduled checks run around every **five minutes on weekdays** (cron `2-59/5 * * * 1-5`, UTC weekday; Monday-Friday UTC). GitHub scheduling is **not exact**; runs can be delayed or skipped. Monitor logs periodically. On public repos inactivity can eventually disable schedules.
6. Confirm the `data/alerts_state.json` file changes following a *real qualifying notification*. The app may redeploy after these state commits.

## Operational caveats
- **Data may be delayed or missing**, so this is not a guaranteed real-time entry/exit service. This is not safe for relying on a timely live stop-loss alert. Stops are notifications, not protective exchange orders.
- The app's entry price is the **confirmation candle's historical close**, not a fill. Exits occur when a later completed candle's OHLC range touches a stop/target; if both levels are touched in the same candle, the system assumes stop first. Missing data can miss exits. Gaps, slippage, fees, contract multipliers, and actual fill conditions aren't modeled.
- An active paper setup is tracked independently for each market/timeframe; no multiple overlapping paper positions for a given market/timeframe. Only new signals on the **last completed candle** within a freshness window trigger entry alerts; there is no automatic historical backfill.
- If Telegram succeeds but GitHub fails to commit the updated state, a future run may repeat a message. Duplicate prevention is best-effort, not mathematically guaranteed exactly-once delivery.
- The repository is public; **alert state is public**. Never put broker credentials, Telegram tokens, or private identifiers in it. No real-money trading or broker connection is implemented.

## Local run
```bash
pip install -r requirements.txt
streamlit run app.py
```

Official documentation: [Telegram Bot API](https://core.telegram.org/bots/api), [GitHub Actions schedule](https://docs.github.com/en/actions/reference/events-that-trigger-workflows#schedule).
