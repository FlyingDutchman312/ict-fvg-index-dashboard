"""V3.1 forward scanner audit: observations are not trading performance."""
from collections import Counter
from datetime import datetime, timezone
import pandas as pd

from strategy import completed_candles, evaluate

ET = 'America/New_York'
MAX_RECORDS = 4000


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def snapshot(symbol, interval, raw, now, outcome, error=None):
    """Diagnose completed candles without inventing a trade or weakening filters."""
    record = {'checked_at_utc': pd.Timestamp(now).tz_convert('UTC').isoformat(),
              'market': symbol, 'timeframe': interval, 'decision': outcome,
              'data_status': 'UNKNOWN', 'bars': 0, 'last_bar_end_et': None,
              'lag_minutes': None, 'total_historical_setups': 0,
              'sweep': False, 'mss': False, 'fvg': False,
              'displacement': False, 'ema_agreement': False,
              'in_session': False, 'last_bar_signal': False, 'error': error}
    if error:
        record['data_status'] = 'ERROR'
        return record
    try:
        bars = completed_candles(raw, interval, now=now)
        record['bars'] = len(bars)
        if bars.empty:
            record['data_status'] = 'NO_DATA'
            return record
        end = bars.index[-1] + pd.Timedelta(minutes=int(interval[:-1]))
        lag = (pd.Timestamp(now).tz_convert(ET) - end).total_seconds()/60
        record['last_bar_end_et'] = end.isoformat()
        record['lag_minutes'] = round(max(0, lag), 1)
        if len(bars) < 75:
            record['data_status'] = 'INSUFFICIENT_BARS'
            return record
        record['data_status'] = 'FRESH' if 0 <= lag <= 2*int(interval[:-1])+2 else 'STALE'
        enriched, signals = evaluate(bars, lookback=12, reward=2.0,
                                    session='New York regular hours (09:30–16:00 ET)',
                                    displacement_atr=0.7, sweep_window=8, cooldown=12)
        last = enriched.iloc[-1]
        record['total_historical_setups'] = len(signals)
        record['sweep'] = bool(last.SweepLow or last.SweepHigh)
        record['mss'] = bool(last.BullMSS or last.BearMSS)
        record['fvg'] = bool(last.BullFVG or last.BearFVG)
        record['displacement'] = bool(last.BullDisplacement or last.BearDisplacement)
        record['ema_agreement'] = bool((last.Close > last.EMA20 > last.EMA50) or
                                        (last.Close < last.EMA20 < last.EMA50))
        record['in_session'] = bool(last.InSession)
        record['last_bar_signal'] = bool(not signals.empty and
                                         int(signals.iloc[-1]['Candle index']) == len(enriched)-1)
    except Exception as exc:
        record['data_status'] = 'ERROR'
        record['error'] = f'{type(exc).__name__}: {exc}'[:350]
    return record


def summarize(records):
    decisions = Counter(r.get('decision', 'UNKNOWN') for r in records)
    data = Counter(r.get('data_status', 'UNKNOWN') for r in records)
    result = {'observations': len(records), 'decisions': dict(decisions),
              'data_status': dict(data), 'markets': sorted({r.get('market', '') for r in records})}
    return result
