"""Side-by-side research replay using the existing scanner and ICT preview."""

import pandas as pd

from ict_paper_signals import paper_picks
from strategy import _session_mask, evaluate

SELECTORS = ("Original FVG/ICT", "ICT sweep → shift → retest")
SLIPPAGE = .25
ROUND_TRIP_FEE = 3.0


def replay(frame, signals, point_value):
    """One independent position. Next-bar open entry, stop first on ambiguous bars."""
    trades = []
    position = None
    for i in range(1, len(frame)):
        bar = frame.iloc[i]
        if position is None and i - 1 in signals:
            side, stop = signals[i - 1]
            direction = 1 if side == "LONG" else -1
            entry = bar.open + direction * SLIPPAGE
            risk = direction * (entry - stop)
            if risk > 0:
                position = (direction, entry, stop, entry + 2 * direction * risk)
        if position is None:
            continue
        direction, entry, stop, target = position
        stop_hit = bar.low <= stop if direction == 1 else bar.high >= stop
        target_hit = bar.high >= target if direction == 1 else bar.low <= target
        if not (stop_hit or target_hit):
            continue
        if stop_hit:
            exit_reference = min(stop, bar.open) if direction == 1 else max(stop, bar.open)
        else:
            exit_reference = target
        exit_price = exit_reference - direction * SLIPPAGE
        trades.append((bar.start_ms, direction * (exit_price - entry) * point_value - ROUND_TRIP_FEE))
        position = None
    return trades, int(position is not None)


def compare(frame, symbol):
    frame = frame.reset_index(drop=True)
    all_trades = {label: [] for label in SELECTORS}
    unresolved = dict.fromkeys(SELECTORS, 0)
    journal = []
    point_value = 5 if symbol == "MES" else 2
    groups = frame.start_ms.diff().ne(60_000).cumsum()
    for _, block in frame.groupby(groups, sort=False):
        if len(block) < 70:
            continue
        block = block.reset_index(drop=True)
        times = pd.to_datetime(block.start_ms, unit="ms", utc=True).dt.tz_convert("America/New_York")
        bars = block[["open", "high", "low", "close"]].rename(columns=str.title)
        bars.index = pd.DatetimeIndex(times)
        _, original = evaluate(bars)
        regular = _session_mask(pd.DatetimeIndex(times), "New York regular hours (09:30–16:00 ET)")
        eligible = block.live & regular
        old = {int(row["Candle index"]): (row["Side"], row["Stop"])
               for _, row in original.iterrows() if eligible.iloc[int(row["Candle index"])]}
        index_by_time = dict(zip(block.start_ms, block.index))
        new = {index_by_time[pick["start_ms"]]: (pick["side"].upper(), pick["stop"])
               for pick in paper_picks(list(block.itertuples(index=False)))
               if eligible.iloc[index_by_time[pick["start_ms"]]]}
        for i in sorted(old.keys() | new.keys()):
            original_side = old[i][0] if i in old else ""
            ict_side = new[i][0] if i in new else ""
            journal.append({"Signal close (ET)": (times.iloc[i] + pd.Timedelta(minutes=1)).isoformat(),
                            SELECTORS[0]: original_side, SELECTORS[1]: ict_side,
                            "Agreement": "Same direction" if original_side == ict_side else
                            "Opposite directions" if original_side and ict_side else "One selector"})
        for label, signals in zip(SELECTORS, (old, new)):
            trades, pending = replay(block, signals, point_value)
            all_trades[label].extend(trades)
            unresolved[label] += pending
    scores = []
    for label, trades in all_trades.items():
        pnl = pd.Series([value for _, value in sorted(trades)], dtype=float)
        equity = pnl.cumsum()
        drawdown = (equity.cummax().clip(lower=0) - equity).max()
        scores.append({"Selector": label, "Closed": len(pnl), "Unresolved": unresolved[label],
                       "Net P&L ($)": round(pnl.sum(), 2) if len(pnl) else None,
                       "Average ($)": round(pnl.mean(), 2) if len(pnl) else None,
                       "Max drawdown ($)": round(drawdown, 2) if len(pnl) else None})
    return pd.DataFrame(scores), pd.DataFrame(journal)
