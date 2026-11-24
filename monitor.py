"""Scheduled conservative ICT/FVG paper-signal Telegram monitor.

No broker integration. Never enters actual trades. State stored as nonsecret JSON
in git. Runs in GitHub Actions every ~5 minutes (not real-time guaranteed).
"""
import copy
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

from strategy import completed_candles, evaluate, prepare
from validation import snapshot, MAX_RECORDS

ET = ZoneInfo('America/New_York')
STATE_PATH = Path(__file__).parent / 'data' / 'alerts_state.json'
SYMBOLS = {'NQ': 'NQ=F', 'ES': 'ES=F', 'QQQ': 'QQQ', 'SPY': 'SPY',
           'NDX': '^NDX', 'SPX': '^GSPC', 'COMP': '^IXIC'}
# Primary futures monitors plus ETFs by default. Indices optional via MONITOR_SYMBOLS.
DEFAULT_SYMBOLS = 'NQ,ES,NDX,SPX'
INTERVALS = ('5m', '15m')
SESSION = 'New York regular hours (09:30–16:00 ET)'


def read_state(path=STATE_PATH):
    if not Path(path).exists():
        return {'version': 1, 'monitors': {}, 'recent_events': []}
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    data.setdefault('monitors', {})
    data.setdefault('recent_events', [])
    return data


def write_state(state, path=STATE_PATH):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    temp = Path(str(path) + '.tmp')
    temp.write_text(json.dumps(state, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    temp.replace(path)


def send_telegram(message, token, chat_id):
    if not token or not chat_id:
        raise RuntimeError('TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID is missing')
    response = requests.post('https://api.telegram.org/bot' + token + '/sendMessage',
                             json={'chat_id': chat_id, 'text': message}, timeout=20)
    response.raise_for_status()
    payload = response.json()
    if not payload.get('ok'):
        raise RuntimeError('Telegram rejected message: ' + str(payload.get('description', 'unknown')))
    return True


def now_et():
    return pd.Timestamp.now(tz=ET)


def timestamp_start(t):
    return pd.Timestamp(t).tz_convert(ET)


def bar_end(t, interval):
    return timestamp_start(t) + pd.Timedelta(minutes=int(interval[:-1]))


def fresh(end, now, interval):
    # Two bars of acceptable quote lag; extended but still not real-time.
    window = pd.Timedelta(minutes=(2 * int(interval[:-1]) + 2))
    return pd.Timedelta(0) <= now - end <= window


def alert_entry(symbol, interval, signal, candle_end):
    return (f'🟢 ICT + FVG PAPER ENTRY — {symbol} {interval} {signal["Side"]}\n'
            f'Confirmation candle closed: {candle_end:%Y-%m-%d %H:%M} ET\n'
            f'Entry reference: {signal["Entry reference"]:,.2f}\n'
            f'Stop-loss reference: {signal["Stop"]:,.2f}\n'
            f'Take-profit reference: {signal["Target"]:,.2f}\n'
            f'Reward-to-risk: {signal["R:R"]}\n'
            f'Confluence: liquidity sweep → MSS → displacement + FVG → EMA trend\n'
            'PAPER ONLY; no order placed. Quote may be delayed; historical close is not a fill.')


def alert_exit(symbol, interval, trade, price, kind, candle_end):
    return (f'🔔 ICT + FVG PAPER EXIT — {symbol} {interval}\n'
            f'{kind} touched on completed candle ending {candle_end:%Y-%m-%d %H:%M} ET\n'
            f'Side: {trade["side"]}\n'
            f'Entry reference: {trade["entry"]:,.2f}\n'
            f'Stop: {trade["stop"]:,.2f} | Target: {trade["target"]:,.2f}\n'
            f'Exit level reference: {price:,.2f}\n'
            'PAPER ONLY. OHLC cannot establish intrabar execution or slippage.')


def exit_from_candles(trade, candles, interval):
    """Checks all completed bars AFTER signal bar, in order. Stop wins same-bar tie."""
    entry_bar = pd.Timestamp(trade['signal_bar_start'])
    if entry_bar.tzinfo is None:
        raise ValueError('Entry bar timestamp must contain timezone')
    for t, candle in candles.iterrows():
        if t <= entry_bar:
            continue
        if trade['side'] == 'LONG':
            stop_hit, target_hit = candle.Low <= trade['stop'], candle.High >= trade['target']
        else:
            stop_hit, target_hit = candle.High >= trade['stop'], candle.Low <= trade['target']
        if stop_hit or target_hit:
            kind = 'STOP-LOSS' if stop_hit else 'TAKE-PROFIT'
            price = trade['stop'] if stop_hit else trade['target']
            return kind, float(price), bar_end(t, interval)
    return None


def fetch(symbol, interval):
    raw = yf.download(SYMBOLS[symbol], period='1mo', interval=interval,
                      auto_adjust=False, prepost=(symbol in ('NQ', 'ES')),
                      progress=False, threads=False, timeout=25)
    return prepare(raw)


def process_symbol(symbol, interval, state, notify, now=None, fetcher=fetch):
    """Mutates state only AFTER notification succeeds; caller persists it.

    Errors propagate to allow nonzero job status and retry on next invocation.
    """
    now = now_et() if now is None else pd.Timestamp(now).tz_convert(ET)
    key = f'{symbol}:{interval}'
    monitor = state['monitors'].setdefault(key, {'active': None, 'last_signal_id': None})
    candles = completed_candles(fetcher(symbol, interval), interval, now=now)
    if len(candles) < 75:
        print(f'{key}: insufficient bars ({len(candles)})')
        return False
    enriched, signals = evaluate(candles, lookback=12, reward=2.0, session=SESSION,
                                 displacement_atr=0.7, sweep_window=8, cooldown=12)
    changed = False
    active = monitor.get('active')
    if active:
        outcome = exit_from_candles(active, enriched, interval)
        if outcome:
            kind, price, exit_end = outcome
            notify(alert_exit(symbol, interval, active, price, kind, exit_end))
            event = {'type': 'EXIT', 'market': symbol, 'timeframe': interval,
                     'side': active['side'], 'outcome': kind, 'price': price,
                     'time_et': exit_end.isoformat(), 'signal_id': active['signal_id']}
            state['recent_events'].append(event)
            monitor['active'] = None
            changed = True
            print(f'{key}: {kind} notification sent')
        else:
            print(f'{key}: active paper setup; no stop or target hit')
        # No new entry during a run that began with a position, even if it exited.
        return changed

    if signals.empty:
        print(f'{key}: no matching setup')
        return False
    signal = signals.iloc[-1]
    signal_bar = int(signal['Candle index'])
    if signal_bar != len(enriched) - 1:
        print(f'{key}: latest setup is historical')
        return False
    signal_start = enriched.index[signal_bar]
    end = bar_end(signal_start, interval)
    if not fresh(end, now, interval):
        print(f'{key}: latest setup stale; no alert')
        return False
    signal_id = f'{key}:{signal_start.isoformat()}:{signal["Side"]}'
    if monitor.get('last_signal_id') == signal_id:
        print(f'{key}: duplicate signal ignored')
        return False
    trade = {'signal_id': signal_id, 'signal_bar_start': signal_start.isoformat(),
             'side': str(signal['Side']), 'entry': float(signal['Entry reference']),
             'stop': float(signal['Stop']), 'target': float(signal['Target']),
             'time_et': end.isoformat()}
    notify(alert_entry(symbol, interval, signal, end))
    monitor['active'] = trade
    monitor['last_signal_id'] = signal_id
    state['recent_events'].append({'type': 'ENTRY', 'market': symbol,
                                   'timeframe': interval, 'side': trade['side'],
                                   'entry': trade['entry'], 'stop': trade['stop'],
                                   'target': trade['target'], 'time_et': end.isoformat(),
                                   'signal_id': signal_id})
    print(f'{key}: entry notification sent')
    return True


def main():
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    chat_id = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
    if not token or not chat_id:
        raise SystemExit('Missing GitHub Actions secrets: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID')
    symbols = [x.strip().upper() for x in os.environ.get('MONITOR_SYMBOLS', DEFAULT_SYMBOLS).split(',') if x.strip()]
    invalid = [x for x in symbols if x not in SYMBOLS]
    if invalid:
        raise SystemExit(f'Invalid MONITOR_SYMBOLS values: {invalid}')
    state = read_state()
    history_file = Path(__file__).parent / 'data' / 'scanner_history.json'
    try:
        history = json.loads(history_file.read_text(encoding='utf-8')) if history_file.exists() else {'version': 1, 'scans': []}
        if not isinstance(history.get('scans'), list):
            raise ValueError('scanner_history.json scans must be a list')
    except (OSError, ValueError) as exc:
        raise SystemExit(f'Forward scan history cannot be loaded safely: {exc}')
    error_count = 0
    for symbol in dict.fromkeys(symbols):
        for interval in INTERVALS:
            now = now_et()
            captured = {'raw': None}
            initial_events = len(state['recent_events'])
            def captured_fetch(market, timeframe):
                captured['raw'] = fetch(market, timeframe)
                return captured['raw']
            try:
                changed = process_symbol(symbol, interval, state,
                                         lambda msg: send_telegram(msg, token, chat_id),
                                         now=now, fetcher=captured_fetch)
                events_added = state['recent_events'][initial_events:]
                if events_added:
                    decision = 'ENTRY_ALERT' if events_added[-1]['type'] == 'ENTRY' else 'EXIT_ALERT'
                elif state['monitors'].get(f'{symbol}:{interval}', {}).get('active'):
                    decision = 'ACTIVE_PAPER_SETUP'
                else:
                    decision = 'NO_FRESH_ENTRY'
                if changed:
                    state['recent_events'] = state['recent_events'][-100:]
                    write_state(state)  # preserve V3 sent-alert ledger
                record = snapshot(symbol, interval, captured['raw'], now, decision)
                if record['data_status'] in ('NO_DATA', 'INSUFFICIENT_BARS', 'ERROR'):
                    print(f'V3.1 {symbol}:{interval}: {record["data_status"]}')
                else:
                    print(f'V3.1 {symbol}:{interval}: {decision}, data={record["data_status"]}, '
                          f'lag={record["lag_minutes"]}m, bars={record["bars"]}')
            except Exception as exc:
                error_count += 1
                reason = f'{type(exc).__name__}: {exc}'
                print(f'ERROR {symbol}:{interval}: {reason}')
                record = snapshot(symbol, interval, captured['raw'], now, 'SCAN_ERROR', error=reason[:350])
            history['scans'].append(record)
    history['scans'] = history['scans'][-MAX_RECORDS:]
    history_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = history_file.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(history, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    tmp.replace(history_file)
    print(f'V3.1 audit persisted: {len(history["scans"])} total retained scanner observations')
    if error_count:
        raise SystemExit(f'{error_count} scanner(s) encountered errors; see logs. Other successful alerts persisted locally.')
    print('Monitor completed: paper only, no brokerage connections')


if __name__ == '__main__':
    main()
