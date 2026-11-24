"""Streamlit forward-validation view. Uses public repo audit, not a private secret."""
import json
from pathlib import Path

import pandas as pd
import requests
import streamlit as st

REPO = 'FlyingDutchman312/ict-fvg-index-dashboard'


@st.cache_data(ttl=90, show_spinner=False)
def get_audit():
    # Public repository; no token in browser or Streamlit secrets.
    url = f'https://raw.githubusercontent.com/{REPO}/main/data/scanner_history.json'
    try:
        response = requests.get(url, timeout=10, headers={'Cache-Control': 'no-cache'})
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get('scans'), list):
            raise ValueError('Unexpected scanner history document')
        return payload, 'GitHub main branch (public, may be cached)'
    except (requests.RequestException, ValueError) as exc:
        local = Path(__file__).parent / 'data' / 'scanner_history.json'
        if local.exists():
            try:
                payload = json.loads(local.read_text(encoding='utf-8'))
                return payload, f'Deployed local file — GitHub fetch unavailable: {exc}'
            except (ValueError, OSError):
                pass
        return {'scans': []}, f'Unavailable: {exc}'


def render():
    with st.expander('📊 V3.1 · Scanner Validation & Forward Observations', expanded=True):
        st.caption('Recorded completed-candle scanner checks, not independent trades. Historical setup counts are diagnostic only. GitHub Actions writes to the public repository; no broker orders.')
        if st.button('Refresh validation history', key='refresh_v31'):
            get_audit.clear()
            st.rerun()
        payload, source = get_audit()
        st.caption(f'Data source: {source}')
        records = payload.get('scans', [])
        if not records:
            st.info('No saved forward observations yet. After uploading V3.1, run the ICT FVG Telegram Paper Alerts workflow once; it will save a scan record even when no entry signal qualifies.')
            return
        df = pd.DataFrame(records)
        if 'checked_at_utc' not in df.columns:
            st.warning('Forward audit has an unexpected format.')
            return
        df['checked_at_utc'] = pd.to_datetime(df['checked_at_utc'], utc=True, errors='coerce')
        df = df.dropna(subset=['checked_at_utc']).sort_values('checked_at_utc')
        if df.empty:
            st.warning('No valid timestamps found in saved observations.')
            return
        latest = df.iloc[-1]['checked_at_utc']
        now = pd.Timestamp.now(tz='UTC')
        age = (now - latest).total_seconds()/60
        c1, c2, c3, c4 = st.columns(4)
        c1.metric('Stored scanner checks', len(df))
        c2.metric('Latest check (ET)', latest.tz_convert('America/New_York').strftime('%b %d %H:%M'))
        c3.metric('Fresh-data checks', int((df.get('data_status', pd.Series(dtype=str)) == 'FRESH').sum()))
        c4.metric('Paper entry alerts', int((df.get('decision', pd.Series(dtype=str)) == 'ENTRY_ALERT').sum()))
        if age > 35:
            st.warning(f'Latest stored scan is {age:.0f} minutes old. This can be normal outside the workflow schedule; review GitHub Actions for current monitoring health.')
        if 'market' in df:
            markets = sorted(df.market.dropna().unique().tolist())
            selected = st.multiselect('Markets to review', markets, default=markets, key='v31_market_filter')
            df = df[df.market.isin(selected)]
        if df.empty:
            st.info('No observations match the selected markets.')
            return
        st.markdown('**Outcomes by scanner**')
        last_per = df.sort_values('checked_at_utc').groupby(['market','timeframe'], as_index=False).tail(1)
        columns = [x for x in ['market','timeframe','checked_at_utc','decision','data_status','bars','lag_minutes','last_bar_signal'] if x in last_per]
        st.dataframe(last_per[columns].sort_values(['market','timeframe']), hide_index=True, use_container_width=True)
        c1, c2 = st.columns(2)
        with c1:
            st.markdown('**Scanner decisions (not trades)**')
            st.bar_chart(df['decision'].fillna('UNKNOWN').value_counts())
        with c2:
            st.markdown('**Latest-candle filter observations**')
            flags = ['sweep','mss','fvg','displacement','ema_agreement','in_session','last_bar_signal']
            counts = {k: int(df[k].fillna(False).astype(bool).sum()) for k in flags if k in df}
            if counts:
                st.bar_chart(pd.Series(counts, name='Checks with condition'))
        st.markdown('**Most recent observations**')
        st.dataframe(df.tail(100).iloc[::-1], hide_index=True, use_container_width=True)
        st.download_button('Download forward scan observations (CSV)', df.to_csv(index=False).encode(),
                           file_name='ict_fvg_v31_forward_scans.csv', mime='text/csv')
        st.info('Milestone gate: before changing filters, collect repeated fresh in-session observations, verify independent alert/exit events, and assess outcome quality. Zero alerts does not prove the system is broken or profitable.')
