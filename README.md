# ICT + FVG Conservative Nasdaq & S&P 500 Streamlit Dashboard

An educational analysis-only intraday scanner. **No trades are placed.** It does not provide validated predictions or actual realized P&L.

## Markets
- Nasdaq-100 E-mini futures: `NQ=F`
- S&P 500 E-mini futures: `ES=F`
- Nasdaq-100 index: `^NDX`
- Nasdaq Composite index: `^IXIC`
- S&P 500 index: `^GSPC`
- QQQ and SPY ETFs.

Intraday Yahoo Finance historical data can be **delayed, incomplete, or unavailable**, especially around futures maintenance or market holidays. For genuine low-latency live signals, integrate a licensed futures market-data vendor and test timestamp semantics before trading. Futures symbols may represent rolled contracts. The default session filter is 09:30–16:00 America/New_York, not the full futures trading day.

## Conservative signal logic

A close-confirmed setup requires (a) a sweep/reclaim against a **previous** 12-bar extreme, (b) a later market structure break above/below the **previous** five-bar extreme within eight bars, (c) a directional three-candle FVG and displacement body >=0.7 ATR on that MSS candle or within the next two bars, (d) EMA20/50 agreement, and (e) the selected New York trading session. A 12-bar cooldown and new-sweep requirement reduce repeat signals. Intraday candle end times are checked, to exclude uncompleted candles. A stale candle will never trigger a new-signal banner. Entry reference is the signal candle close; price execution at that historical close is **not** assumed.

Important: The displayed FVGs are 8-bar visual annotations, not tracked unmitigated/filled zones. The signal table is not a backtest, and no strategy win rate is claimed. Historical OHLC cannot determine all stop/target intrabar ordering, and the model omits costs and futures margin effects.

## Deploy
1. Create a **separate** GitHub repository, e.g. `ict-fvg-index-dashboard` (leave the existing BTC/Kalshi app as is).
2. Upload `app.py`, `strategy.py`, `requirements.txt`, and this `README.md` to the **repository root** (not inside a nested folder).
3. Open https://share.streamlit.io, choose **Create app** > **Yup, I have an app** and connect that repository.
4. Choose `main` branch and `app.py` file path, then Deploy. No secrets or keys are required.
5. On failures, open **Manage app > Logs**, then inspect dependency and quote errors.

Locally: `pip install -r requirements.txt` then `streamlit run app.py`.
