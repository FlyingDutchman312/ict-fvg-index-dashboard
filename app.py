"""Nasdaq / S&P 500 conservative ICT + FVG educational dashboard."""
from datetime import datetime
from pathlib import Path
import hmac
import json
import re
from zoneinfo import ZoneInfo
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yfinance as yf
from strategy import prepare, completed_candles, evaluate

ET = ZoneInfo('America/New_York')
st.set_page_config(page_title='ICT + FVG | Nasdaq & S&P 500', page_icon='📈', layout='wide')
st.title('📈 Nasdaq & S&P 500 | ICT + Fair Value Gap')
st.caption('V3 · Conservative intraday scanner · 5m / 15m · Eastern Time · Telegram paper alerts via GitHub Actions · No automatic orders')


def show_worker_charts():
    try:
        url = str(st.secrets.get("FVG_SUPABASE_URL", "")).strip().rstrip("/")
        key = str(st.secrets.get("FVG_SUPABASE_SECRET_KEY", "")).strip()
        password = str(st.secrets.get("FVG_DASHBOARD_PASSWORD", ""))
    except FileNotFoundError:
        url = key = password = ""
    with st.expander("MES and MNQ · Railway candle charts", expanded=True):
        if not password or not key or not re.fullmatch(r"https://[a-z0-9-]+\.supabase\.co", url):
            st.info("Railway charts require the FVG Supabase URL, server key, and dashboard password in Streamlit Secrets.")
            return
        if not st.session_state.get("fvg_chart_authorized", False):
            with st.form("fvg_chart_unlock"):
                entered = st.text_input("Chart password", type="password")
                unlock = st.form_submit_button("Unlock charts")
            if not unlock:
                return
            if not hmac.compare_digest(entered.encode(), password.encode()):
                st.error("Incorrect chart password.")
                return
            st.session_state["fvg_chart_authorized"] = True
        if st.button("Lock charts"):
            st.session_state["fvg_chart_authorized"] = False
            st.rerun()
        from fvg_charts import render_charts
        render_charts(url, key)


show_worker_charts()

MARKETS = {
    'Nasdaq-100 futures (E-mini NQ)': ('NQ=F', 'Futures'),
    'S&P 500 futures (E-mini ES)': ('ES=F', 'Futures'),
    'Nasdaq-100 cash index': ('^NDX', 'Cash index'),
    'Nasdaq Composite cash index': ('^IXIC', 'Cash index'),
    'S&P 500 cash index': ('^GSPC', 'Cash index'),
    'Nasdaq-100 ETF (QQQ)': ('QQQ', 'ETF'),
    'S&P 500 ETF (SPY)': ('SPY', 'ETF'),
}
SESSIONS = ['New York regular hours (09:30–16:00 ET)',
            'New York AM (09:30–11:30 ET)',
            'New York PM (13:30–16:00 ET)', 'All sessions']

@st.cache_data(ttl=120, show_spinner=False)
def load_prices(ticker: str, interval: str, use_extended: bool):
    raw = yf.download(ticker, period='1mo', interval=interval, auto_adjust=False,
                      prepost=use_extended, progress=False, threads=False, timeout=20)
    return prepare(raw)

with st.sidebar:
    st.header('Trading setup')
    name = st.selectbox('Instrument', list(MARKETS), index=0)
    ticker, kind = MARKETS[name]
    interval = st.selectbox('Chart timeframe', ['5m', '15m'], index=1)
    session = st.selectbox('ICT trading session (New York time)', SESSIONS)
    st.caption('Conservative filter enabled: sweep → structure shift → displacement + FVG + EMA trend.')
    lookback = st.slider('Liquidity lookback (bars)', 8, 24, 12)
    rr = st.select_slider('Reference reward:risk', options=[1.5, 2.0, 2.5, 3.0], value=2.0)
    bars = st.slider('Candles displayed', 70, 250, 140, step=10)
    show_gaps = st.checkbox('Show fair value gap zones', True)
    show_sweeps = st.checkbox('Show liquidity sweep markers', True)
    use_extended = st.checkbox('Include extended-hours data when available', value=(kind == 'Futures'))
    if st.button('↻ Refresh quotes', use_container_width=True):
        load_prices.clear()
        st.rerun()
    st.divider()
    st.info('Quotes are fetched from Yahoo Finance and may be delayed or unavailable. No API keys, brokers, execution, or guaranteed real-time feed.')

try:
    raw = load_prices(ticker, interval, use_extended)
except Exception as exc:
    st.error(f'Unable to retrieve quotes for {ticker}: {exc}')
    st.info('Try another instrument, a different timeframe, or refresh later.')
    st.stop()
if raw.empty:
    st.warning('No candles returned. Yahoo intraday history can be temporarily unavailable.')
    st.stop()
d = completed_candles(raw, interval)
if len(d) < 75:
    st.warning('Not enough completed bars returned to calculate the conservative setup. Try another instrument.')
    st.stop()
d, signals = evaluate(d, lookback=lookback, reward=rr, session=session,
                      displacement_atr=0.7, sweep_window=8, cooldown=12)
last = d.iloc[-1]
last_end = d.index[-1] + pd.Timedelta(minutes=int(interval[:-1]))
now = datetime.now(ET)
stale = (pd.Timestamp(now) - last_end) > pd.Timedelta(minutes=max(15, 3*int(interval[:-1])))
fresh = (not signals.empty and int(signals.iloc[-1]['Candle index']) == len(d)-1 and not stale)
setup = signals.iloc[-1] if fresh else None

c1, c2, c3, c4 = st.columns(4)
c1.metric('Last completed close', f'{last.Close:,.2f}')
c2.metric('EMA trend', 'BULLISH' if last.EMA20 > last.EMA50 else 'BEARISH')
c3.metric('Latest setup', setup.Side if fresh else 'WAIT')
c4.metric('Candle closed (ET)', last_end.strftime('%a %b %d %H:%M'))
if stale:
    st.warning(f'**Data freshness warning:** most recent completed candle closed at {last_end:%Y-%m-%d %H:%M} ET. Market may be closed or quotes delayed. Historical signals are NOT current entry alerts.')
elif fresh:
    st.success(f'**New historical-bar {setup.Side} setup:** close reference {setup["Entry reference"]:,.2f} · stop {setup.Stop:,.2f} · target {setup.Target:,.2f} · R:R {setup["R:R"]}. Do not interpret this as a live executable order.')
else:
    st.info('No fresh conservative setup on the last completed candle. WAIT means the required confirmations have not aligned.')

visible = d.tail(bars)
fig = go.Figure()
fig.add_trace(go.Candlestick(x=visible.index, open=visible.Open, high=visible.High,
                             low=visible.Low, close=visible.Close, name='Price'))
for col, color in [('EMA20', '#ffc857'), ('EMA50', '#65baff')]:
    fig.add_trace(go.Scatter(x=visible.index, y=visible[col], mode='lines', name=col,
                             line={'color': color, 'width': 1.5}))
if show_gaps:
    plotted = 0
    for t, r in visible.iterrows():
        for bull in (True, False):
            if plotted >= 24:
                break
            if not (bool(r.BullFVG) if bull else bool(r.BearFVG)):
                continue
            lo = r.BullGapLow if bull else r.BearGapLow
            hi = r.BullGapHigh if bull else r.BearGapHigh
            pos = visible.index.get_loc(t)
            end = min(pos+8, len(visible)-1)
            if end <= pos:
                continue
            fig.add_shape(type='rect', x0=t, x1=visible.index[end], y0=float(lo), y1=float(hi),
                          line={'width': 0}, fillcolor='rgba(33,188,125,0.15)' if bull else 'rgba(238,86,101,0.15)')
            plotted += 1
if show_sweeps:
    for col, side, color, price in [('SweepLow', 'triangle-up', '#2dcd9b', 'Low'),
                                    ('SweepHigh', 'triangle-down', '#f56f7f', 'High')]:
        points = visible[visible[col]]
        fig.add_trace(go.Scatter(x=points.index, y=points[price], mode='markers', name=col,
                                 marker={'symbol': side, 'size': 9, 'color': color}))
if not signals.empty:
    first = len(d)-len(visible)
    for side, symbol, color in [('LONG', 'arrow-up', '#21ca8e'), ('SHORT', 'arrow-down', '#fc6376')]:
        subset = signals[(signals.Side == side) & (signals['Candle index'] >= first)]
        if subset.empty:
            continue
        fig.add_trace(go.Scatter(x=[d.index[int(i)] for i in subset['Candle index']],
                                 y=subset['Entry reference'], mode='markers', name=side+' signal',
                                 marker={'symbol': symbol, 'size': 15, 'color': color,
                                         'line': {'color': '#fff', 'width': 1}}))
fig.update_layout(template='plotly_dark', height=610, margin={'l': 10, 'r': 10, 't': 20, 'b': 15},
                  xaxis_title='America/New_York (ET)', yaxis_title='Price (points or USD)',
                  yaxis={'side': 'right'}, xaxis_rangeslider_visible=False,
                  legend={'orientation': 'h'})
st.plotly_chart(fig, use_container_width=True)
st.caption('Zones are three-candle FVGs extended eight chart bars for visibility, not audited unmitigated gaps. Green/red arrows are conservative historical setup references, not trades.')

main, help_col = st.columns([2, 1])
with main:
    st.subheader('Conservative signal history')
    if signals.empty:
        st.write('No signals matched all filters in this historical data window.')
    else:
        show = signals.drop(columns='Candle index').iloc[::-1].copy()
        st.dataframe(show, hide_index=True, use_container_width=True)
        st.download_button('Download signals CSV', show.to_csv(index=False).encode(),
                           file_name=f'ict_fvg_{ticker.replace("=", "_").replace("^", "")}_{interval}.csv',
                           mime='text/csv')
with help_col:
    st.subheader('Conservative entry rules')
    st.markdown('''1. Prior liquidity high/low is swept and the candle closes back inside.
2. Within **8 bars**, a subsequent directional **market-structure shift** breaks a prior 5-bar structure level.
3. On the MSS bar or next **2 bars**, a **3-candle FVG** and **displacement body ≥ 0.7 × ATR** form in that direction.
4. EMA20 and EMA50 agree with the direction; signal candle falls inside the selected NY session.
5. Minimum **12-bar cooldown** prevents repeated nearby alerts.

**Entry reference** = completed confirmation candle close. **Stop** = sweep-to-signal extreme or 1.2 × ATR, whichever is farther. **Target** = selected risk multiple.''')
    st.caption('No assumption of fills, slippage, commission, margin, contract size, walk-forward validation, or profitability.')

st.divider()
st.subheader('📨 Telegram paper alerts and exit tracking')
st.caption('The separate GitHub Actions monitor runs on a schedule even when this Streamlit browser tab is closed. It does not run inside Streamlit. Alerts require Telegram secrets and an enabled workflow. A scheduled check can arrive late or fail.')
state_file = Path(__file__).parent / 'data' / 'alerts_state.json'
if state_file.exists():
    try:
        alert_state = json.loads(state_file.read_text(encoding='utf-8'))
        active_rows = []
        for key, info in alert_state.get('monitors', {}).items():
            trade = info.get('active')
            if trade:
                active_rows.append({'Market : timeframe': key, 'Side': trade['side'],
                                    'Entry reference': trade['entry'], 'Stop': trade['stop'],
                                    'Target': trade['target'], 'Signal time (ET)': trade['time_et']})
        if active_rows:
            st.markdown('**Active hypothetical paper setups**')
            st.dataframe(pd.DataFrame(active_rows), hide_index=True, use_container_width=True)
        else:
            st.info('No tracked open paper setups in the latest repository state.')
        events = alert_state.get('recent_events', [])
        if events:
            st.markdown('**Recent Telegram event log**')
            st.dataframe(pd.DataFrame(events[-20:][::-1]), hide_index=True, use_container_width=True)
        else:
            st.caption('No alerts have been recorded yet. This does not confirm the monitor is running; check GitHub → Actions.')
    except (OSError, ValueError, KeyError) as exc:
        st.warning(f'Unable to read alert-state file: {exc}')
else:
    st.info('No alert-state file yet; upload data/alerts_state.json and configure the GitHub Actions workflow.')
st.caption('The dashboard shows the state available in its deployed Git revision; GitHub may have a newer state. Monitor NQ, ES, Nasdaq-100 and S&P 500 cash indices on both 5m and 15m by default; other supported symbols can be enabled in the workflow.')

st.divider()
st.warning('Educational research tool only. ICT definitions are subjective; these are transparent mechanical proxies. Yahoo continuous futures symbols can include roll adjustments and delayed/incomplete quotes. Cash indices cannot be traded directly; NQ/ES futures and ETFs have different market hours, sizing, and risks. No live orders or broker credentials.')
