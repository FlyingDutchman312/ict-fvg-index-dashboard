"""Read-only FVG shadow-scenario performance and observation-health dashboard.

No brokerage APIs, database writes, or order execution.
"""

import hmac
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

from fvg_charts import render_charts
from dashboard_core import prepare, metrics, daily_risk, forward_test_scorecard

st.set_page_config(page_title='FVG Shadow Performance', page_icon='📊', layout='wide')
UTC = timezone.utc
EASTERN = ZoneInfo('America/New_York')
COLUMNS = ('trade_key,instrument,contract_symbol,direction,status,opened_utc,closed_utc,'
           'entry_price,exit_price,stop,target,exit_reason,estimated_risk_usd,'
           'net_pnl_usd,fee_per_side_usd,slippage_ticks,quote_exchange_time_verified,'
           'is_real_fill,model,created_at')
OBS_COLUMNS = ('instrument,contract_symbol,created_at,detected_at,'
               'paper_enabled,quote_exchange_time_verified')


def secret(name):
    """Read Railway environment variables first, then Streamlit secrets."""
    value = os.getenv(name, '').strip()
    if value:
        return value
    try:
        return str(st.secrets.get(name, '')).strip()
    except Exception:
        return ''


def et_time(value):
    """Convert a Supabase timestamptz to ET, without guessing on malformed input."""
    if not value:
        return '—'
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return 'Unspecified timezone'
        return parsed.astimezone(EASTERN).strftime('%b %d, %Y · %I:%M:%S %p %Z')
    except (ValueError, TypeError, OverflowError):
        return 'Invalid timestamp'


def utc_datetime(value):
    """Return aware UTC datetime, or None for untrusted/unparseable timestamps."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def age_text(minutes):
    if minutes is None:
        return 'Unknown'
    if minutes < 0:
        return 'Future timestamp (check clock)'
    if minutes < 1:
        return 'Under 1 minute'
    if minutes < 60:
        return f'{int(minutes)} min'
    if minutes < 1440:
        return f'{minutes / 60:.1f} hr'
    return f'{minutes / 1440:.1f} days'


def safe_get(endpoint, headers, params):
    response = requests.get(endpoint, headers=headers, params=params, timeout=18)
    response.raise_for_status()
    result = response.json()
    if not isinstance(result, list):
        raise ValueError('Unexpected Supabase response')
    return result


@st.cache_data(ttl=30, show_spinner=False)
def get_scenarios(project_url, api_key, lookback):
    """Read only; paginate to avoid Supabase's common default 1,000-row cap."""
    endpoint = project_url.rstrip('/') + '/rest/v1/fvg_shadow_scenarios'
    headers = {'apikey': api_key, 'Authorization': 'Bearer ' + api_key,
               'Accept': 'application/json'}
    params = {'select': COLUMNS, 'order': 'opened_utc.desc'}
    if lookback != 'All':
        days = {'7 days': 7, '30 days': 30, '90 days': 90}[lookback]
        params['opened_utc'] = 'gte.' + (datetime.now(UTC) - timedelta(days=days)).isoformat()
    output = []
    for offset in range(0, 10000, 1000):
        batch = safe_get(endpoint, headers, {**params, 'limit': 1000, 'offset': offset})
        output.extend(batch)
        if len(batch) < 1000:
            return output, False
    return output, True


@st.cache_data(ttl=20, show_spinner=False)
def get_latest_observations(project_url, api_key):
    """Latest *stored signal observation* for each instrument; not a worker heartbeat."""
    endpoint = project_url.rstrip('/') + '/rest/v1/fvg_forward_observations'
    headers = {'apikey': api_key, 'Authorization': 'Bearer ' + api_key,
               'Accept': 'application/json'}
    latest = {}
    for instrument in ('MES', 'MNQ'):
        rows = safe_get(endpoint, headers, {
            'select': OBS_COLUMNS,
            'instrument': 'eq.' + instrument,
            'order': 'created_at.desc',
            'limit': 1,
        })
        latest[instrument] = rows[0] if rows else None
    return latest


st.title('FVG · MES / MNQ')
st.caption('Phase 10 · Forward-testing, shadow-strategy research and observation monitoring only · no broker trades or verified exchange-time quotes')
st.warning('Entries, exits, and P&L are **hypothetical OHLC-based scenarios**, not fills or realized returns. '
           'Quote exchange timestamps have not been verified. A small sample of hypothetical scenarios does not demonstrate profitability.',
           icon='⚠️')

password = secret('FVG_DASHBOARD_PASSWORD')
if not password:
    st.error('Dashboard access is locked. Configure FVG_DASHBOARD_PASSWORD in Railway Variables or Streamlit secrets.')
    st.stop()
if not st.session_state.get('fvg_authorized', False):
    attempt = st.text_input('Dashboard password', type='password')
    if st.button('Unlock', type='primary'):
        if attempt and hmac.compare_digest(attempt, password):
            st.session_state['fvg_authorized'] = True
            st.rerun()
        else:
            st.error('Incorrect password')
    st.stop()

url = secret('FVG_SUPABASE_URL')
key = secret('FVG_SUPABASE_SECRET_KEY')
if not url.startswith('https://') or '.supabase.co' not in url or not key:
    st.error('Configure FVG_SUPABASE_URL and FVG_SUPABASE_SECRET_KEY in Railway Variables or Streamlit secrets. '
             'Never paste server-side keys into GitHub files.')
    st.stop()

render_charts(url, key)

st.subheader('System health · MES / MNQ')
st.caption('Live-refreshing *stored FVG observations* (every 30 seconds). No new FVG signal is not proof of a stopped worker. '
           'This panel cannot verify Railway process health, exchange quote timestamps, or live market-data continuity.')
threshold = st.selectbox('Observation recency reference', [15, 30, 60, 120], index=2,
                         format_func=lambda x: f'{x} minutes',
                         help='A display-only reference. Sparse FVG signals can exceed this age while the worker is healthy.')


def show_health(project_url, api_key, recency_minutes):
    # st.fragment, if installed, reruns the health panel independently.
    checked_at = datetime.now(UTC)
    try:
        latest = get_latest_observations(project_url, api_key)
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else 'unknown'
        st.error(f'Observation database query failed (HTTP {code}). Check permissions to fvg_forward_observations.')
        st.caption('Cannot infer worker status from a failed database query.')
        return
    except (requests.RequestException, ValueError, TypeError):
        st.error('Could not retrieve stored observations from Supabase. No credentials are displayed.')
        return

    cols = st.columns(2)
    for i, instrument in enumerate(('MES', 'MNQ')):
        row = latest.get(instrument)
        with cols[i]:
            st.markdown(f'**{instrument} · last stored observation**')
            if row is None:
                st.info('No stored observations found for this instrument.')
                continue
            stored = utc_datetime(row.get('created_at'))
            age_minutes = ((checked_at - stored).total_seconds() / 60) if stored else None
            if age_minutes is None or age_minutes < -1:
                st.warning('Timestamp unavailable or inconsistent')
            elif age_minutes <= recency_minutes:
                st.success('Recent signal observation')
            else:
                st.warning('No recent FVG observation — worker status unconfirmed')
            st.metric('Age of latest stored observation', age_text(age_minutes))
            st.caption('Stored (Eastern): ' + et_time(row.get('created_at')))
            st.caption('Detected (Eastern): ' + et_time(row.get('detected_at')))
            st.caption('Contract: ' + str(row.get('contract_symbol') or 'Unknown'))
            verified = row.get('quote_exchange_time_verified') is True
            st.caption('Quote exchange timestamp: ' + ('flagged verified' if verified else 'unverified'))
            paper = row.get('paper_enabled')
            st.caption('Observation paper_enabled flag: ' + str(paper))
    st.caption('Last database check (Eastern): ' + checked_at.astimezone(EASTERN).strftime('%b %d, %Y · %I:%M:%S %p %Z'))
    if st.button('↻ Refresh observation health', key='refresh_health'):
        get_latest_observations.clear()
        st.rerun()


if hasattr(st, 'fragment'):
    @st.fragment(run_every='30s')
    def health_fragment():
        show_health(url, key, threshold)
    health_fragment()
else:
    st.info('Automatic panel refresh requires a Streamlit version supporting st.fragment. Manual refresh remains available.')
    show_health(url, key, threshold)

st.divider()
controls = st.columns([1, 1, 1, 1])
with controls[0]:
    symbol = st.selectbox('Instrument', ['Both', 'MES', 'MNQ'])
with controls[1]:
    window = st.selectbox('Period', ['30 days', '7 days', '90 days', 'All'])
with controls[2]:
    state = st.selectbox('Scenario status', ['All', 'CLOSED', 'OPEN'])
with controls[3]:
    st.write('')
    if st.button('↻ Refresh performance', use_container_width=True):
        get_scenarios.clear()
        st.rerun()

try:
    with st.spinner('Reading shadow scenario journal…'):
        # The forward-test scorecard must not be limited by the user's display filters.
        # Read the available archive once and apply legacy dashboard filters locally.
        raw, truncated = get_scenarios(url, key, 'All')
except requests.HTTPError as exc:
    code = exc.response.status_code if exc.response is not None else 'unknown'
    st.error(f'Supabase scenario read failed (HTTP {code}). Verify the table and server-side key permissions.')
    st.stop()
except (requests.RequestException, ValueError, TypeError):
    st.error('Unable to read shadow scenarios from Supabase. Check Railway variables and service availability.')
    st.stop()

if truncated:
    st.warning('Reached 10,000 scenarios; results may be incomplete. Select a shorter date range.')
all_df = prepare(raw)
df = all_df.copy()
if window != 'All' and not df.empty:
    cutoff = pd.Timestamp.now(tz='UTC') - pd.Timedelta(days={'7 days': 7, '30 days': 30, '90 days': 90}[window])
    df = df[df['opened_utc'] >= cutoff]

if symbol != 'Both':
    df = df[df['instrument'] == symbol]
if state != 'All':
    df = df[df['status'] == state]

m = metrics(df)
def money(x):
    return f'${x:,.2f}' if x >= 0 else f'−${abs(x):,.2f}'
def pct(x):
    return '—' if x is None else f'{100*x:.1f}%'
def pftext(x):
    return '—' if x is None else ('∞ (no losses)' if x == float('inf') else f'{x:.2f}')

st.subheader('Phase 10 · Forward-test scorecard')
st.caption('Based on persisted hypothetical outcomes only · target: 50 scored MES and 50 scored MNQ. '
           'A 100-scenario milestone is for research, not proof of an edge. '
           'This section ignores the performance display filters below and uses all retrieved records.')
progress, rolling, audit_flags = forward_test_scorecard(
    all_df, target_per_symbol=50, max_risk_usd=10.0,
    daily_loss_usd=50.0, max_scenarios_per_day=5)
scored_total = int(progress['Scored completed'].sum())
st.progress(min(1.0, scored_total / 100.0),
            text=f'{scored_total} of 100 scored completed hypothetical scenarios ({min(scored_total, 100)}% initial milestone)')
progress_cols = st.columns(2)
for i, symbol_name in enumerate(('MES', 'MNQ')):
    record = progress.loc[progress['Instrument'] == symbol_name].iloc[0]
    with progress_cols[i]:
        st.markdown(f'**{symbol_name} forward-test progress**')
        st.progress(min(1.0, int(record['Scored completed']) / 50.0),
                    text=f"{int(record['Scored completed'])}/50 scored · {int(record['Remaining'])} remaining")
        st.caption(f"Open: {int(record['Open'])} · CLOSED without P&L: {int(record['Closed missing P&L'])} "
                   f"· CLOSED missing close timestamp: {int(record['Closed missing timestamp'])}")

st.markdown('**Seven-day and thirty-day performance (by UTC close time)**')
rolling_view = rolling.copy()
rolling_view['Win rate'] = rolling_view['Win rate'].apply(pct)
rolling_view['Profit factor'] = rolling_view['Profit factor'].apply(pftext)
st.dataframe(rolling_view, hide_index=True, use_container_width=True)

with st.expander('Forward-test record completeness and historical risk-policy audit', expanded=False):
    missing = int(progress['Closed missing P&L'].sum())
    no_close_time = int(progress['Closed missing timestamp'].sum())
    st.write(f'Closed records missing theoretical P&L: **{missing}** · '
             f'Closed records missing close timestamp: **{no_close_time}**')
    st.caption('Flags compare all stored historical scenarios with today\'s reference settings '
               '($10 estimated maximum per scenario, $50 daily theoretical net-loss reference, '
               'five scenarios per instrument per UTC day). Older scenarios may predate these settings. '
               'Historical overshoots do not by themselves prove a worker risk-gate failure. '
               'Neither the dashboard nor these flags enforce trading limits.')
    if audit_flags.empty:
        st.success('No historical reference-limit exceptions detected in retrieved scenarios.')
    else:
        st.warning(f'{len(audit_flags)} historical reference-limit exception(s) found. Review their context.')
        st.dataframe(audit_flags, use_container_width=True, hide_index=True)
    if truncated:
        st.warning('The 10,000-row retrieval limit means these scorecard totals and audits may be incomplete.')

st.subheader('Hypothetical performance')
a, b, c, d, e = st.columns(5)
a.metric('Net theoretical P&L', money(m['net']))
b.metric('Win rate', pct(m['win_rate']))
c.metric('Profit factor', pftext(m['profit_factor']))
d.metric('Closed scenarios', m['closed'])
e.metric('Open scenarios', m['open'])
f, g, h = st.columns(3)
f.metric('Max theoretical drawdown', money(m['drawdown']))
g.metric('Gross gains', money(m['gross_wins']))
h.metric('Gross losses', money(m['gross_losses']))
if m['closed'] != m.get('scored', 0):
    st.info(f"{m['closed'] - m.get('scored', 0)} CLOSED record(s) lack P&L and were excluded from return metrics.")

st.subheader('Daily shadow risk — UTC day')
st.caption('Based on opened_utc and net_pnl_usd; not an active broker limit or live Railway telemetry. '
           'Historical trades can exceed current limits.')
risk_cols = st.columns(2)
for i, s in enumerate(('MES', 'MNQ')):
    r = daily_risk(all_df, s, cap=50.0, max_scenarios=5)
    with risk_cols[i]:
        st.markdown(f'**{s}** · {r["entries"]}/5 scenarios today · {money(r["net"])} theoretical realized today')
        st.progress(max(0.0, min(1.0, r['remaining'] / 50.0)),
                    text=f'Estimated remaining loss budget: {money(r["remaining"])}')
        if r['blocked']:
            st.caption('Research entries blocked by daily cutoff or count (derived estimate).')

st.subheader('Theoretical equity curve')
closed = df[(df.status == 'CLOSED') & df.net_pnl_usd.notna()].copy()
if closed.empty:
    st.info('No closed shadow scenarios in the selected filters.')
else:
    closed = closed.sort_values(['closed_utc', 'trade_key'])
    closed['cumulative_net_usd'] = closed['net_pnl_usd'].cumsum()
    plot = closed[['closed_utc', 'cumulative_net_usd']].dropna().set_index('closed_utc')
    if not plot.empty:
        st.line_chart(plot, y='cumulative_net_usd', height=270)
    closed['utc_day'] = closed['closed_utc'].dt.strftime('%Y-%m-%d')
    daily = closed.groupby('utc_day')['net_pnl_usd'].sum()
    st.caption('Daily theoretical net P&L (UTC)')
    st.bar_chart(daily, height=200)

st.subheader('MES vs MNQ comparison')
compare = []
for s in ('MES', 'MNQ'):
    sm = metrics(df[df.instrument == s])
    compare.append({'Instrument': s, 'Closed': sm['closed'], 'Open': sm['open'],
                    'Theoretical net P&L': round(sm['net'], 2), 'Win rate': pct(sm['win_rate']),
                    'Profit factor': pftext(sm['profit_factor'])})
st.dataframe(pd.DataFrame(compare), hide_index=True, use_container_width=True)

st.subheader('Performance by instrument and direction')
if closed.empty:
    st.info('No closed shadow scenarios to compare for these filters.')
else:
    # Group scored scenarios only. Breakeven scenarios count in the denominator.
    direction = closed['direction'].astype('string').str.strip().str.upper()
    directional = closed.assign(
        Direction=direction.mask(direction.eq('')).fillna('UNKNOWN'),
        Won=closed['net_pnl_usd'].gt(0),
    )
    breakdown = directional.groupby(['instrument', 'Direction'], dropna=False).agg(
        **{'Scored': ('net_pnl_usd', 'size'),
           'Wins': ('Won', 'sum'),
           'Theoretical net P&L ($)': ('net_pnl_usd', 'sum'),
           'Average per scenario ($)': ('net_pnl_usd', 'mean')},
    ).reset_index().rename(columns={'instrument': 'Instrument'})
    breakdown['Win rate (%)'] = (100 * breakdown['Wins'] / breakdown['Scored']).round(1)
    st.caption('Uses closed hypothetical scenarios with recorded P&L in the current filters.')
    st.dataframe(breakdown.round(2), hide_index=True, use_container_width=True)

st.subheader('Scenario journal')
if df.empty:
    st.info('No scenarios match the current filters. This is not necessarily a data error.')
else:
    fields = ['instrument', 'contract_symbol', 'direction', 'status', 'opened_utc', 'closed_utc',
              'entry_price', 'stop', 'target', 'exit_price', 'exit_reason', 'estimated_risk_usd',
              'net_pnl_usd', 'is_real_fill', 'quote_exchange_time_verified', 'trade_key']
    view = df[[c for c in fields if c in df.columns]].sort_values('opened_utc', ascending=False)
    st.dataframe(view, use_container_width=True, hide_index=True, height=350)
    st.download_button('Download filtered CSV', view.to_csv(index=False).encode('utf-8'),
                       file_name='fvg_hypothetical_scenarios.csv', mime='text/csv')

st.divider()
st.caption('Phase 10 · Read-only Supabase dashboard · source tables: public.fvg_shadow_scenarios and '
           'public.fvg_forward_observations · health panel checks every 30 seconds when supported · '
           'performance data cached for 30 seconds · no Railway log API or brokerage orders. '
           'Keep server-side Supabase keys out of GitHub.')
if st.button('Lock dashboard'):
    st.session_state['fvg_authorized'] = False
    st.rerun()
