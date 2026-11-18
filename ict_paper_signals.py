"""Causal sweep, structure shift and FVG retest research rules."""

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

TICK = Decimal("0.25")
LOOKBACK = 10
EXPIRY = 12


def paper_picks(candles):
    picks = []
    group_start = 0
    setup = None
    for i, bar in enumerate(candles):
        if i and bar.start_ms - candles[i - 1].start_ms != 60_000:
            group_start = i
            setup = None
        if i - group_start < LOOKBACK:
            continue
        if setup:
            index, side, extreme, structure, shifted, gap = setup
            invalid = bar.low <= extreme if side == 1 else bar.high >= extreme
            if invalid or i - index > EXPIRY:
                setup = None
                continue
            if gap:
                low, high = gap
                invalid = bar.close < low if side == 1 else bar.close > high
                retest = bar.low <= high and bar.close >= high if side == 1 else bar.high >= low and bar.close <= low
                if invalid:
                    setup = None
                elif retest:
                    entry = Decimal(str(bar.close))
                    rounding = ROUND_FLOOR if side == 1 else ROUND_CEILING
                    stop = (Decimal(str(extreme)) / TICK).to_integral_value(rounding=rounding) * TICK - side * TICK
                    target = entry + 2 * (entry - stop)
                    if bar.live:
                        picks.append(dict(start_ms=bar.start_ms, side="Long" if side == 1 else "Short",
                                          entry=float(entry), stop=float(stop), target=float(target),
                                          reason="Liquidity sweep → structure shift → FVG retest"))
                    setup = None
                continue
            shifted = shifted or (bar.close > structure if side == 1 else bar.close < structure)
            if shifted and i - index >= 2:
                first = candles[i - 2]
                if side == 1 and bar.low > first.high:
                    gap = (first.high, bar.low)
                elif side == -1 and bar.high < first.low:
                    gap = (bar.high, first.low)
            setup = (index, side, extreme, structure, shifted, gap)
            continue
        history = candles[i - LOOKBACK:i]
        low = min(item.low for item in history)
        high = max(item.high for item in history)
        swept_low = bar.low < low < bar.close
        swept_high = bar.high > high > bar.close
        if swept_low != swept_high:
            setup = ((i, 1, bar.low, high, False, None) if swept_low
                     else (i, -1, bar.high, low, False, None))
    return picks
