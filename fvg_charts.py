"""Read-only, contract-specific MES/MNQ charts for the research dashboard."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

EASTERN = ZoneInfo("America/New_York")


@st.cache_data(ttl=30, show_spinner=False)
def completed_candles(url, key, instrument):
    headers = {"apikey": key}
    if not key.startswith(("sb_secret_", "sb_publishable_")):
        headers["Authorization"] = "Bearer " + key
    response = requests.get(
        url.rstrip("/") + "/rest/v1/fvg_chart_candles", headers=headers,
        params={"instrument": f"eq.{instrument}", "order": "start_ms.desc,live.desc,contract_symbol.asc",
                "limit": 400, "select": "contract_symbol,start_ms,open,high,low,close,live"},
        timeout=8, allow_redirects=False,
    )
    if response.status_code != 200:
        raise ValueError(f"Candle history unavailable (HTTP {response.status_code})")
    rows = response.json()
    if not isinstance(rows, list):
        raise ValueError("Unexpected candle history response")
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    return (frame.loc[frame.contract_symbol.eq(frame.iloc[0].contract_symbol)]
            .sort_values("start_ms").reset_index(drop=True))


def gaussian_channel(candles, span=20, width=2.0):
    previous_close = candles.close.shift()
    true_range = pd.concat([candles.high - candles.low,
                            (candles.high - previous_close).abs(),
                            (candles.low - previous_close).abs()], axis=1).max(axis=1)
    smooth = pd.DataFrame({"Center": candles.close, "Range": true_range})
    for _ in range(4):
        smooth = smooth.ewm(span=span, adjust=False).mean()
    margin = width * smooth["Range"]
    return smooth["Center"], smooth["Center"] + margin, smooth["Center"] - margin


def render_charts(url, key):
    for label, symbol in (("Nasdaq-100 — MNQ", "MNQ"), ("S&P 500 — MES", "MES")):
        st.subheader(label)
        channel = st.checkbox("Gaussian Channel", value=True, key=f"gaussian_{symbol}")
        st.markdown("[Open ICT Concepts by LuxAlgo on TradingView]"
                    "(https://www.tradingview.com/script/ib4uqBJx-ICT-Concepts-LuxAlgo/)")
        try:
            frame = completed_candles(url, key, symbol)
        except (requests.RequestException, ValueError, KeyError, TypeError):
            st.warning("Candle history unavailable. Check the chart migration and worker logs.")
            continue
        if frame.empty:
            st.info("Waiting for completed one-minute candles from Railway.")
            continue
        times = pd.to_datetime(frame.start_ms, unit="ms", utc=True).dt.tz_convert(EASTERN)
        labels = times.dt.strftime("%b %d %H:%M %Z")
        fig = go.Figure(go.Candlestick(x=labels, open=frame.open, high=frame.high,
                                       low=frame.low, close=frame.close, name=symbol))
        if channel and len(frame) >= 20:
            for name, values in zip(("Gaussian center", "Upper band", "Lower band"),
                                    gaussian_channel(frame)):
                fig.add_scatter(x=labels, y=values, mode="lines", name=name)
        elif channel:
            st.caption("Gaussian Channel appears after 20 completed candles.")
        fig.update_layout(xaxis_type="category", xaxis_nticks=8, xaxis_title="Eastern Time",
                          yaxis_title="Index points", xaxis_rangeslider_visible=False, height=400)
        st.plotly_chart(fig, use_container_width=True, key=f"completed_candles_{symbol}")
        age = datetime.now(timezone.utc) - (times.iloc[-1] + pd.Timedelta(minutes=1))
        st.caption(f"{frame.contract_symbol.iloc[-1]} · latest stored candle closed "
                   f"{times.iloc[-1] + pd.Timedelta(minutes=1):%b %d %H:%M %Z} · "
                   f"{max(0, int(age.total_seconds() / 60))} minutes ago. Includes validated replay when available.")
