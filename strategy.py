"""Causal, educational ICT-inspired conservative intraday setup scanner.

Each row represents a *completed* vendor OHLC candle. The model is NOT a
validated prediction algorithm. No future candles are used to form a signal.
"""
from datetime import time
import numpy as np
import pandas as pd

ET = 'America/New_York'


def prepare(raw: pd.DataFrame) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()
    d = raw.copy()
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    d.columns = [str(c).title() for c in d.columns]
    if any(c not in d.columns for c in ('Open', 'High', 'Low', 'Close')):
        return pd.DataFrame()
    d = d[['Open', 'High', 'Low', 'Close']].apply(pd.to_numeric, errors='coerce').dropna()
    d = d[(d.High >= d.Low) & (d.High >= d[['Open', 'Close']].max(axis=1)) &
          (d.Low <= d[['Open', 'Close']].min(axis=1))].sort_index()
    d = d[~d.index.duplicated(keep='last')]
    if d.index.tz is None:
        # yfinance typically delivers timezone-aware intraday data; never assume
        # naive timestamps are local exchange time.
        d.index = d.index.tz_localize('UTC')
    d.index = d.index.tz_convert(ET)
    return d


def completed_candles(d: pd.DataFrame, interval: str, now=None) -> pd.DataFrame:
    """Candle timestamps from yfinance denote candle START, not its close."""
    if d.empty:
        return d.copy()
    current = pd.Timestamp.now(tz=ET) if now is None else pd.Timestamp(now)
    current = current.tz_localize(ET) if current.tzinfo is None else current.tz_convert(ET)
    candle_width = pd.Timedelta(minutes=int(interval.removesuffix('m')))
    # Extra 10 seconds protects against receiving a just-closing candle.
    return d.loc[d.index + candle_width + pd.Timedelta(seconds=10) <= current].copy()


def _session_mask(index: pd.DatetimeIndex, session: str) -> np.ndarray:
    if session == 'All sessions':
        return np.ones(len(index), dtype=bool)
    clocks = index.tz_convert(ET).time
    if session == 'New York AM (09:30–11:30 ET)':
        return np.asarray([(time(9, 30) <= t < time(11, 30)) for t in clocks])
    if session == 'New York PM (13:30–16:00 ET)':
        return np.asarray([(time(13, 30) <= t < time(16, 0)) for t in clocks])
    # Only trade regular US hours by default; bars at 16:00 or beyond excluded.
    return np.asarray([(time(9, 30) <= t < time(16, 0)) for t in clocks])


def evaluate(data: pd.DataFrame, lookback: int = 12, reward: float = 2.0,
             session: str = 'New York regular hours (09:30–16:00 ET)',
             displacement_atr: float = 0.7, sweep_window: int = 8,
             cooldown: int = 12):
    """Return enriched candles and historical, non-executable setup references.

    Conservative sequence:
      (1) prior-window liquidity sweep and reclaim,
      (2) subsequent market-structure break (MSS) within 8 candles,
      (3) directional displacement and a three-candle FVG on that MSS bar
          or in the following 2 candles,
      (4) EMA20/EMA50 directional agreement, and specified NY session.
    Entry reference is signal-bar close; it is NOT a future limit/market fill.
    """
    d = data.copy()
    cols = ['Time (ET)', 'Side', 'Entry reference', 'Stop', 'Target', 'R:R',
            'Setup details', 'Candle index']
    if len(d) < max(lookback + 55, 70):
        return d, pd.DataFrame(columns=cols)
    prev = d.Close.shift(1)
    tr = pd.concat([d.High - d.Low, (d.High-prev).abs(), (d.Low-prev).abs()], axis=1).max(axis=1)
    d['ATR'] = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    d['EMA20'] = d.Close.ewm(span=20, adjust=False, min_periods=20).mean()
    d['EMA50'] = d.Close.ewm(span=50, adjust=False, min_periods=50).mean()
    prior_hi = d.High.rolling(lookback, min_periods=lookback).max().shift(1)
    prior_lo = d.Low.rolling(lookback, min_periods=lookback).min().shift(1)
    d['SweepLow'] = (d.Low < prior_lo) & (d.Close > prior_lo)
    d['SweepHigh'] = (d.High > prior_hi) & (d.Close < prior_hi)
    # Break of recent *prior* structure; never references current future high/low.
    structure_hi = d.High.rolling(5, min_periods=5).max().shift(1)
    structure_lo = d.Low.rolling(5, min_periods=5).min().shift(1)
    d['BullMSS'] = d.Close > structure_hi
    d['BearMSS'] = d.Close < structure_lo
    body = (d.Close - d.Open).abs()
    d['BullDisplacement'] = (d.Close > d.Open) & (body >= displacement_atr*d.ATR)
    d['BearDisplacement'] = (d.Close < d.Open) & (body >= displacement_atr*d.ATR)
    d['BullFVG'] = d.Low > d.High.shift(2)
    d['BearFVG'] = d.High < d.Low.shift(2)
    d['BullGapLow'] = d.High.shift(2).where(d.BullFVG)
    d['BullGapHigh'] = d.Low.where(d.BullFVG)
    d['BearGapLow'] = d.High.where(d.BearFVG)
    d['BearGapHigh'] = d.Low.shift(2).where(d.BearFVG)
    d['InSession'] = _session_mask(d.index, session)
    d['LONG'] = False
    d['SHORT'] = False

    signals = []
    last_signal_i = -cooldown - 1
    # These rolling state variables track chronological sequence without lookahead.
    last_sweep = {'LONG': -99999, 'SHORT': -99999}
    last_mss = {'LONG': -99999, 'SHORT': -99999}
    for i in range(len(d)):
        r = d.iloc[i]
        if bool(r.SweepLow):
            last_sweep['LONG'] = i
            last_mss['LONG'] = -99999  # a fresh sweep requires a fresh MSS
        if bool(r.SweepHigh):
            last_sweep['SHORT'] = i
            last_mss['SHORT'] = -99999
        for side in ('LONG', 'SHORT'):
            if (i > last_sweep[side] and i-last_sweep[side] <= sweep_window and
                    ((side == 'LONG' and bool(r.BullMSS)) or
                     (side == 'SHORT' and bool(r.BearMSS)))):
                last_mss[side] = i
            valid_sequence = (0 <= i-last_mss[side] <= 2 and
                              0 < last_mss[side]-last_sweep[side] <= sweep_window)
            if not valid_sequence or not bool(r.InSession) or pd.isna(r.ATR):
                continue
            if side == 'LONG':
                valid = (bool(r.BullFVG) and bool(r.BullDisplacement) and
                         r.Close > r.EMA20 > r.EMA50)
            else:
                valid = (bool(r.BearFVG) and bool(r.BearDisplacement) and
                         r.Close < r.EMA20 < r.EMA50)
            if not valid or i-last_signal_i <= cooldown:
                continue
            entry, atr = float(r.Close), float(r.ATR)
            # Includes sweep candle, if within window; conservative extrema.
            segment = d.iloc[max(0, last_sweep[side]):i+1]
            if side == 'LONG':
                stop = min(float(segment.Low.min()), entry-1.2*atr)
                risk = entry-stop
                target = entry+reward*risk
            else:
                stop = max(float(segment.High.max()), entry+1.2*atr)
                risk = stop-entry
                target = entry-reward*risk
            if not np.isfinite([entry, stop, risk, target]).all() or risk <= 0:
                continue
            d.at[d.index[i], side] = True
            signals.append({
                'Time (ET)': (d.index[i] + pd.Timedelta(minutes=0)).strftime('%Y-%m-%d %H:%M'),
                'Side': side, 'Entry reference': round(entry, 2),
                'Stop': round(stop, 2), 'Target': round(target, 2),
                'R:R': f'1:{reward:g}', 'Setup details':
                f'Sweep {i-last_sweep[side]} bars ago; MSS {i-last_mss[side]} bars ago; FVG + displacement',
                'Candle index': i,
            })
            last_signal_i = i
            # Require a NEW sweep and MSS for another setup of same direction.
            last_sweep[side] = -99999
            last_mss[side] = -99999
            break
    return d, pd.DataFrame(signals, columns=cols)
