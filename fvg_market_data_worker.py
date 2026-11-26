#!/usr/bin/env python3
"""Phase 16.3.1 reconstructed DXLink quote freshness DIAGNOSTIC worker (not original source).

Read-only tastytrade REST (OAuth + quote-token) and DXLink market-data subscriptions.
No broker/account/order endpoints. Paper execution is NEVER enabled by this file.
Shadow research, candidate journal persistence, internal paper simulation, and
broker execution are disabled and explicitly rejected in this diagnostic release.

Dry run: python -u fvg_market_data_worker.py --self-test
Startup safety: FVG_STREAM_ENABLED must be explicitly true for network streaming;
FVG_ENABLE_SHADOW_RESEARCH defaults false. All execution-related switches MUST
be disabled or worker refuses to start. This is a new implementation; test against
provider feed before replacing a running service.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import logging
import math
import os
import random
import signal
import sys
import time
from urllib.parse import urlparse

import requests
from candle_validation import ValidatedCandleStream
from fvg_engine import Bar, PaperFVG
from candidate_journal import CandidateJournal
from observation_store import ObservationStore
from candle_chart_store import CandleChartStore
from shadow_engine import ShadowEngine, ShadowStore

class AuthenticationFailure(RuntimeError):
    pass

LOG = logging.getLogger('fvg.market_data')
VERSION = 'phase16.3.2-negotiated-feed-decoder-v1'
QUOTE_FIELDS = ('eventType', 'eventSymbol', 'bidTime', 'askTime', 'bidPrice', 'askPrice')
CANDLE_FIELDS = ('eventType', 'eventSymbol', 'eventFlags', 'time', 'open', 'high', 'low', 'close', 'volume')
USER_AGENT = 'fvg-phase16-research/1.0'
STOP = False


def env_true(name: str, default: str = 'false') -> bool:
    return os.getenv(name, default).strip().lower() == 'true'


def require_safe_mode() -> None:
    """Reject ambiguous or unsafe switches, including legacy deployment flags."""
    must_be_false = ('FVG_LIVE_TRADING_ENABLED', 'FVG_ENABLE_PAPER_SIM',
                     'PAPER_ENABLED', 'ORDERS_ENABLED', 'BROKER_ORDERS',
                     'FVG_PAPER_ENABLED', 'FVG_ORDERS_ENABLED', 'FVG_BROKER_ORDERS',
                     'LIVE_TRADING_ENABLED', 'PRODUCTION_TRADING_ENABLED',
                     'FVG_ENABLE_INTERNAL_PAPER', 'FVG_ENABLE_ORDERS',
                     'FVG_ENABLE_SHADOW_RESEARCH')
    # Fail closed for arbitrary unexpected non-false strings.
    bad = [k for k in must_be_false if os.getenv(k, 'false').strip().lower() not in ('false', '0', 'off', 'no', '')]
    if os.getenv('FVG_PAPER_ONLY', 'true').strip().lower() != 'true':
        bad.append('FVG_PAPER_ONLY')
    if bad:
        raise RuntimeError('Safety gate: refusing enabled/ambiguous execution flags: ' + ', '.join(bad))
    LOG.info('SAFETY_POLICY paper_enabled=false orders_enabled=false broker_orders=false real_fills=0')


def normalize_quote_epoch_ms(value):
    """Accept only plausibly ranged integer epoch seconds/milliseconds; never infer packed DXFeed times."""
    if isinstance(value, bool):
        return None, 'invalid'
    if isinstance(value, int):
        v = value
    elif isinstance(value, str) and value.isdecimal() and len(value) <= 16:
        v = int(value)
    else:
        return None, 'missing' if value is None else 'invalid'
    if 1_600_000_000 <= v <= 4_000_000_000:
        return v * 1000, 'seconds'
    if 1_600_000_000_000 <= v <= 4_000_000_000_000:
        return v, 'milliseconds'
    return None, 'invalid'


def quote_age_diagnostic(bid_time, ask_time, received_ms):
    times = [normalize_quote_epoch_ms(x) for x in (bid_time, ask_time)]
    if any(unit == 'missing' for _, unit in times):
        return 'missing_timestamp', None
    if any(ms is None for ms, _ in times):
        return 'invalid_timestamp', None
    ages = [received_ms - ms for ms, _ in times]
    if min(ages) < -5000:
        return 'future_timestamp', max(ages)
    max_age = max(ages)
    return ('fresh' if max_age <= 10_000 else 'stale'), max_age


def safe_sample(value):
    if type(value) is int and len(str(value)) <= 24:
        return str(value)
    if type(value) is str and len(value) <= 24 and value.isdecimal():
        return value
    return 'unavailable'


def symbols_from_env():
    """Require explicit provider streamer symbols; never guess expiring futures contracts."""
    names = {}
    for label in ('MES', 'MNQ'):
        symbol = os.getenv('FVG_' + label + '_STREAMER_SYMBOL', '').strip()
        if not symbol or not symbol.startswith('/') or len(symbol) > 100 or any(x in symbol for x in (' ', '{', '}')):
            raise RuntimeError('Missing/invalid FVG_' + label + '_STREAMER_SYMBOL; use tastytrade streamer-symbol')
        names[label] = symbol
    if names['MES'] == names['MNQ']:
        raise RuntimeError('MES/MNQ streamer symbols must differ')
    return names


class JournalCoordinator:
    """Research-only coordination; no internal paper simulation or broker integration."""
    def __init__(self, symbols, enable_shadow=False):
        self.symbols = dict(symbols)
        self.engines = {s: PaperFVG(s, enabled=False, event_sink=(self._gap_sink if enable_shadow else None)) for s in symbols}
        self.candidates = CandidateJournal() if enable_shadow else None
        self.observations = ObservationStore() if enable_shadow else None
        self.shadow = {}
        if enable_shadow:
            for label, contract in symbols.items():
                self.shadow[label] = ShadowEngine(label, contract,
                    store=ShadowStore(), fee_side=1.5, slip_ticks=1,
                    max_risk_usd=10, daily_loss_usd=50, max_trades_day=5,
                    candidate_sink=self.candidates.submit)
        LOG.info('PHASE16_3_CANDIDATE_JOURNAL enabled=%s broker_orders=False',
                 bool(self.candidates and self.candidates.enabled))
        LOG.info('RESEARCH_POLICY shadow_enabled=%s internal_paper_enabled=False broker_orders=False',
                 bool(self.shadow))

    def _gap_sink(self, event):
        if self.observations is None or event.get('event') != 'FVG_DETECTED':
            return
        label = event.get('symbol')
        contract = self.symbols.get(label)
        if contract:
            self.observations.record_gap(event, contract_symbol=contract, fee_side_usd=1.5,
                                          slip_ticks=1, tick_size=0.25, multiplier=5.0 if label == 'MES' else 2.0)

    def on_completed(self, label, bar, live):
        chart_store = getattr(self, "chart_store", None)
        if chart_store is not None:
            chart_store.record(label, bar, live)
        engine = self.engines[label]
        engine.on_closed_bar(bar, live=live)
        shadow = self.shadow.get(label)
        if shadow:
            shadow.on_bar(bar, live=live, engine=engine)

    def on_quote(self, label, bid, ask):
        # Quotes are for logging only: do not feed unverified quote times into entry models.
        pass

    def summary(self):
        return {name: e.summary() for name, e in self.shadow.items()}


class DataProcessor:
    def __init__(self, symbols, coordinator, start_ms=None):
        self.symbols = symbols
        self.reverse = {v: k for k, v in symbols.items()}
        self.coordinator = coordinator
        self.start_ms = start_ms or int(time.time()*1000)
        self.streams = {s: ValidatedCandleStream(s, self.start_ms) for s in symbols}
        self.received = Counter()
        self.freshness = Counter()
        self.units = Counter()
        self.sampled = set()
        self.last_quote_log = {}
        self.last_receive_mono = {}
        self.last_receive_wall_ms = {}
        self.last_exchange_freshness = {}
        self.last_quote_provider_timestamp_present = {}
        self.zero_time_count = Counter()
        self.last_summary = time.monotonic()
        self.event_fields = {}
        self.decode_stats = Counter()
        self.candle_sampled = set()

    def configure_feed(self, payload):
        """Decode only the server-confirmed COMPACT schema, never requested offsets."""
        if payload.get('dataFormat') != 'COMPACT':
            raise RuntimeError('DXLink unsupported data format')
        incoming = payload.get('eventFields')
        if not isinstance(incoming, dict):
            raise RuntimeError('DXLink event schema missing')
        fields = dict(self.event_fields)
        required = {
            'Quote': {'eventSymbol', 'bidPrice', 'askPrice'},
            'Candle': {'eventSymbol', 'eventFlags', 'time', 'open', 'high', 'low', 'close', 'volume'},
        }
        for kind, needed in required.items():
            if kind in incoming:
                names = incoming[kind]
                if (not isinstance(names, list) or not 1 <= len(names) <= 128 or
                        not all(isinstance(name, str) and 0 < len(name) <= 64 for name in names) or
                        len(set(names)) != len(names) or not needed.issubset(names)):
                    raise RuntimeError('DXLink event schema invalid for ' + kind)
                fields[kind] = tuple(names)
            if kind not in fields:
                raise RuntimeError('DXLink event schema missing for ' + kind)
        self.event_fields = fields
        self.decode_stats['schema_updates'] += 1
        LOG.info('DXLINK_FEED_SCHEMA %s', json.dumps(fields, separators=(',', ':')))

    def quote(self, row):
        symbol = row.get('eventSymbol')
        label = self.reverse.get(symbol) or (symbol if symbol in ('SPY', 'QQQ') else None)
        if not label:
            return
        try:
            bid, ask = float(row.get('bidPrice')), float(row.get('askPrice'))
            if not all(math.isfinite(v) for v in (bid, ask)) or not (0 < bid <= ask):
                return
        except (TypeError, ValueError):
            return
        self.received[label] += 1
        received_ms = int(time.time()*1000)
        b, a = row.get('bidTime'), row.get('askTime')
        freshness, age = quote_age_diagnostic(b, a, received_ms)
        self.freshness[(label, freshness)] += 1
        if b == 0 or a == 0:
            self.zero_time_count[label] += 1
        self.last_receive_mono[label] = time.monotonic()
        self.last_receive_wall_ms[label] = received_ms
        self.last_exchange_freshness[label] = freshness
        self.last_quote_provider_timestamp_present[label] = (
            normalize_quote_epoch_ms(b)[0] is not None and
            normalize_quote_epoch_ms(a)[0] is not None
        )
        _, bu = normalize_quote_epoch_ms(b)
        _, au = normalize_quote_epoch_ms(a)
        self.units[(label, 'bid', bu)] += 1
        self.units[(label, 'ask', au)] += 1
        if label not in self.sampled:
            LOG.info('QUOTE_TIMESTAMP_RAW_SAMPLE %s bidTime=%s askTime=%s received_ms=%d bid_unit=%s ask_unit=%s bid_type=%s ask_type=%s freshness=%s',
                     label, safe_sample(b), safe_sample(a), received_ms, bu, au,
                     type(b).__name__, type(a).__name__, freshness)
            self.sampled.add(label)
        if time.monotonic() - self.last_quote_log.get(label, 0) >= 120:
            LOG.info('Quote %s bid=%.4f ask=%.4f spread=%.4f freshness=%s oldest_side_age_ms=%s exchange_verified=false reception_observed=true',
                     label, bid, ask, ask-bid, freshness, age if age is not None else 'unavailable')
            self.last_quote_log[label] = time.monotonic()
        self.coordinator.on_quote(label, bid, ask)

    def candle(self, row):
        sym = str(row.get('eventSymbol', ''))
        if not sym.endswith('{=m}'):
            return
        label = self.reverse.get(sym[:-4])
        if not label:
            return
        try:
            ts = row.get('time')
            if type(ts) is not int:  # no floating-point / implicit truncation
                return
            bar = Bar(ts, *(float(row[k]) for k in ('open','high','low','close','volume')))
            flags = row.get('eventFlags', 0)
            if type(flags) is not int:
                self.decode_stats['invalid_candle_flags'] += 1
                return
        except (ValueError, TypeError, KeyError):
            return
        self.received[label + '_candles'] += 1
        received_ms = int(time.time()*1000)
        if label not in self.candle_sampled:
            LOG.info('CANDLE_TIMESTAMP_RAW_SAMPLE %s time=%s received_ms=%d minute_aligned=%s event_flags=%d',
                     label, safe_sample(ts), received_ms, ts % 60_000 == 0, flags)
            self.candle_sampled.add(label)
        events, _ = self.streams[label].ingest(bar, received_ms, flags=flags)
        for completed, live in events:
            self.coordinator.on_completed(label, completed, live)

    def feed_data(self, data):
        self.decode_stats['frames'] += 1
        if not isinstance(data, list) or len(data) % 2:
            self.decode_stats['malformed_frames'] += 1
            return
        # dxFeed COMPACT packs ALL events of a type in one flat value array.
        # Split on the width of the negotiated schema before decoding any row.
        i = 0
        while i + 1 < len(data):
            kind, rows = data[i], data[i+1]
            i += 2
            if not isinstance(kind, str) or kind not in ('Quote', 'Candle'):
                self.decode_stats['unknown_event_groups'] += 1
                continue
            fields = self.event_fields.get(kind)
            if not fields:
                self.decode_stats['unconfigured_event_groups'] += 1
                continue
            width = len(fields)
            if not isinstance(rows, list):
                self.decode_stats['malformed_event_groups'] += 1
                continue
            if not rows:
                continue
            # Retain compatibility with explicitly nested rows; reject partial
            # groups rather than silently dropping a tail or shifting offsets.
            if isinstance(rows[0], list):
                batch = rows
            elif len(rows) % width == 0:
                batch = [rows[offset:offset+width] for offset in range(0, len(rows), width)]
            else:
                self.decode_stats['malformed_event_groups'] += 1
                continue
            if any(not isinstance(row, list) or len(row) != width or
                   any(isinstance(value, (dict, list)) for value in row) for row in batch):
                self.decode_stats['malformed_event_groups'] += 1
                continue
            for values in batch:
                row = dict(zip(fields, values))
                if row.get('eventType', kind) != kind:
                    self.decode_stats['event_type_mismatch'] += 1
                    continue
                self.decode_stats[kind + '_decoded'] += 1
                if kind == 'Quote':
                    self.quote(row)
                elif kind == 'Candle':
                    self.candle(row)

    def diagnostics(self, force=False):
        if not force and time.monotonic() - self.last_summary < 120:
            return
        self.last_summary = time.monotonic()
        labels = ('MES','MNQ','SPY','QQQ')
        freshness = {s:{k:self.freshness[(s,k)] for k in ('fresh','stale','missing_timestamp','invalid_timestamp','future_timestamp')} for s in labels}
        units = {s:{side:{k:self.units[(s,side,k)] for k in ('seconds','milliseconds','missing','invalid')}
                     for side in ('bid','ask')} for s in labels}
        # Monotonic clock measures whether quotes CONTINUE to reach this worker.
        # Receipt freshness never promotes an unverified exchange timestamp to 'fresh'.
        now_mono = time.monotonic()
        reception = {}
        provenance = {}
        for label in labels:
            last_mono = self.last_receive_mono.get(label)
            elapsed_ms = max(0, int((now_mono - last_mono) * 1000)) if last_mono is not None else None
            state = ('no_quotes' if elapsed_ms is None else
                     'recent' if elapsed_ms <= 10_000 else 'delayed')
            reception[label] = dict(status=state, age_since_last_received_ms=elapsed_ms,
                                    last_received_wall_ms=self.last_receive_wall_ms.get(label),
                                    quote_count=self.received[label])
            provenance[label] = dict(exchange_timestamp_verified=False,
                                     exchange_freshness=self.last_exchange_freshness.get(label, 'no_quotes'),
                                     provider_bid_ask_timestamps_present=self.last_quote_provider_timestamp_present.get(label, False),
                                     zero_bid_or_ask_time_count=self.zero_time_count[label])
        payload = dict(streaming=True,
                       received={s:self.received[s+'_candles'] for s in ('MES','MNQ')},
                       validation={s:stream.summary() for s,stream in self.streams.items()},
                       feed_decoder=dict(self.decode_stats),
                       quote_freshness=freshness, quote_time_units=units,
                       quote_reception=reception, quote_provenance=provenance,
                       shadow_scenarios=self.coordinator.summary(),
                       internal_paper_sim={'MES':{'mode':'DISABLED','broker_orders':False},
                                           'MNQ':{'mode':'DISABLED','broker_orders':False}},
                       paper_enabled=False, orders_enabled=False, broker_orders=False)
        LOG.info('CANDLE_VALIDATION %s', json.dumps(payload, separators=(',', ':')))


def _fetch_json(method, url, headers=None, payload=None):
    r = requests.request(method, url, headers=headers, json=payload, timeout=15)
    if r.status_code >= 400:
        if '/oauth/token' in url or r.status_code in (401, 403):
            raise AuthenticationFailure('Read-only API authentication rejected, HTTP ' + str(r.status_code))
        raise RuntimeError('Read-only tastytrade API error HTTP ' + str(r.status_code))
    return r.json()


def auth_and_quote_token():
    """OAuth refresh and GET quote token only; no account or order endpoints."""
    secret = os.getenv('TASTYTRADE_CLIENT_SECRET', '')
    refresh = os.getenv('TASTYTRADE_REFRESH_TOKEN', '')
    client_id = os.getenv('TASTYTRADE_CLIENT_ID', '')
    if not secret or not refresh:
        raise RuntimeError('Missing TASTYTRADE_CLIENT_SECRET or TASTYTRADE_REFRESH_TOKEN')
    sandbox = env_true('TASTYTRADE_SANDBOX', 'false')
    host = 'https://api.cert.tastyworks.com' if sandbox else 'https://api.tastyworks.com'
    data = {'grant_type':'refresh_token', 'refresh_token':refresh, 'client_secret':secret}
    if client_id:
        data['client_id'] = client_id
    obj = _fetch_json('POST', host + '/oauth/token',
                      headers={'User-Agent':USER_AGENT,'Content-Type':'application/json'},payload=data)
    access = obj.get('access_token')
    if not isinstance(access, str) or not access:
        raise RuntimeError('OAuth access token missing')
    quote = _fetch_json('GET', host + '/api-quote-tokens',
                        headers={'User-Agent':USER_AGENT,'Authorization':'Bearer '+access})
    q = quote.get('data') or {}
    url, token = q.get('dxlink-url'), q.get('token')
    if not isinstance(token, str) or not token or not isinstance(url, str):
        raise RuntimeError('Tastytrade quote token/URL missing')
    parsed = urlparse(url)
    if parsed.scheme != 'wss' or not parsed.hostname or not parsed.hostname.endswith('.dxfeed.com'):
        raise RuntimeError('Unexpected DXLink endpoint; refusing connection')
    return url, token


def dxlink_session(url, token, processor, symbols):
    try:
        import websocket  # websocket-client
    except ImportError as exc:
        raise RuntimeError('Install websocket-client in requirements.txt') from exc
    ws = websocket.create_connection(url, timeout=15, enable_multithread=True)
    phase = 'setup'
    channel = 3
    last_keepalive = time.monotonic()
    subscribed = False
    try:
        ws.send(json.dumps({'type':'SETUP','channel':0,'version':'0.1-DXF-JS/0.3.0',
                            'keepaliveTimeout':60,'acceptKeepaliveTimeout':60}))
        ws.settimeout(5)
        while not STOP:
            if time.monotonic() - last_keepalive >= 25:
                ws.send(json.dumps({'type':'KEEPALIVE','channel':0}))
                last_keepalive = time.monotonic()
            if subscribed:
                processor.diagnostics()
            try:
                msg = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not msg:
                raise RuntimeError('DXLink connection closed')
            try:
                payload = json.loads(msg)
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            typ = payload.get('type')
            if typ == 'AUTH_STATE':
                state = payload.get('state')
                if state == 'UNAUTHORIZED' and phase == 'setup':
                    ws.send(json.dumps({'type':'AUTH','channel':0,'token':token}))
                    phase = 'auth'
                elif state == 'AUTHORIZED' and phase in ('auth','setup'):
                    ws.send(json.dumps({'type':'CHANNEL_REQUEST','channel':channel,
                                        'service':'FEED','parameters':{'contract':'AUTO'}}))
                    phase = 'channel'
                elif state not in ('AUTHORIZED','UNAUTHORIZED'):
                    raise RuntimeError('DXLink authorization failed (details suppressed)')
            elif typ == 'CHANNEL_OPENED' and payload.get('channel') == channel and phase == 'channel':
                ws.send(json.dumps({'type':'FEED_SETUP','channel':channel,
                                    'acceptAggregationPeriod':0.1,'acceptDataFormat':'COMPACT',
                                    'acceptEventFields':{'Quote':list(QUOTE_FIELDS),
                                                         'Candle':list(CANDLE_FIELDS)}}))
                phase = 'feed_setup'
            elif typ == 'FEED_CONFIG' and payload.get('channel') == channel:
                processor.configure_feed(payload)
                if phase != 'feed_setup':
                    continue
                now_ms = int(time.time()*1000)
                add = []
                for s in ('SPY','QQQ'):
                    add.append({'type':'Quote','symbol':s})
                for s in symbols.values():
                    add.append({'type':'Quote','symbol':s})
                    add.append({'type':'Candle','symbol':s+'{=m}','fromTime':now_ms-30*60_000})
                ws.send(json.dumps({'type':'FEED_SUBSCRIPTION','channel':channel,
                                    'reset':True,'add':add}))
                subscribed = True
                phase = 'streaming'
                LOG.info('DXLINK_SUBSCRIBED futures=MES,MNQ equities=SPY,QQQ candle_interval=1m broker_orders=false')
            elif typ == 'FEED_DATA' and payload.get('channel') == channel:
                processor.feed_data(payload.get('data'))
            elif typ == 'ERROR':
                raise RuntimeError('DXLink ERROR received (sensitive details suppressed)')
            elif typ == 'CHANNEL_CLOSED' and payload.get('channel') == channel:
                raise RuntimeError('DXLink feed channel closed')
    finally:
        ws.close()


def local_self_test():
    """Offline-only tests; never fetch network data or write a Supabase row."""
    require_safe_mode()
    now = int(time.time()*1000)
    assert normalize_quote_epoch_ms(now//1000)[1] == 'seconds'
    assert normalize_quote_epoch_ms(now)[1] == 'milliseconds'
    assert normalize_quote_epoch_ms(0)[1] == 'invalid'
    assert normalize_quote_epoch_ms(None)[1] == 'missing'
    assert quote_age_diagnostic(now-500,now-800,now)[0] == 'fresh'
    assert quote_age_diagnostic(0,0,now)[0] == 'invalid_timestamp'
    class Dummy:
        def __init__(self):self.events=[]
        def on_quote(self,*args):self.events.append(('quote',args))
        def on_completed(self,*args):self.events.append(('bar',args))
        def summary(self):return {}
    obj = DataProcessor({'MES':'/TESTMES:XCME','MNQ':'/TESTMNQ:XCME'},Dummy(),start_ms=now)
    obj.configure_feed({'dataFormat':'COMPACT', 'eventFields': {
        'Quote': list(QUOTE_FIELDS), 'Candle': list(CANDLE_FIELDS)}})
    obj.feed_data(['Quote', ['Quote','/TESTMES:XCME',now-200,now-100,5000.0,5000.25]])
    assert obj.received['MES'] == 1
    assert obj.freshness[('MES','fresh')] == 1
    obj.feed_data(['Quote', ['Quote','/TESTMES:XCME',0,0,5000.0,5000.25]])
    assert obj.received['MES'] == 2
    assert obj.freshness[('MES','invalid_timestamp')] == 1
    assert obj.last_exchange_freshness['MES'] == 'invalid_timestamp'
    assert obj.last_quote_provider_timestamp_present['MES'] is False
    assert obj.last_receive_mono['MES'] > 0
    assert obj.zero_time_count['MES'] == 1
    obj.diagnostics(force=True)
    LOG.info('SELF_TEST_PASS mode=offline no_network=true no_orders=true')


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',stream=sys.stdout)
    parser = argparse.ArgumentParser(description='Phase16.3 reconstructed, execution-disabled worker')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        local_self_test()
        return 0
    require_safe_mode()
    LOG.warning('RECONSTRUCTED_WORKER version=%s original_source=false paper_enabled=false orders_enabled=false broker_orders=false',VERSION)
    if not env_true('FVG_STREAM_ENABLED'):
        LOG.warning('STREAM_DISABLED FVG_STREAM_ENABLED is not true; safe idle mode')
        return 0
    symbols = symbols_from_env()
    # Explicitly prohibit shadow/candidate writes in this diagnostic release.
    coordinator = JournalCoordinator(symbols, enable_shadow=False)
    coordinator.chart_store = CandleChartStore(ObservationStore(), symbols)
    import atexit
    atexit.register(coordinator.chart_store.close)
    backoff = 5.0
    while not STOP:
        try:
            url,token = auth_and_quote_token()
            processor = DataProcessor(symbols,coordinator)
            dxlink_session(url,token,processor,symbols)
            backoff = 5.0
        except AuthenticationFailure:
            LOG.error('AUTHENTICATION_FAILED no_auto_retries=true (verify credentials and authorization privately)')
            return 3
        except Exception as exc:
            # No exception message: avoid accidentally printing credential-bearing URLs.
            LOG.warning('FEED_SESSION_FAILURE error_type=%s reconnect_seconds=%d',type(exc).__name__,int(backoff))
            if not STOP:
                time.sleep(backoff + random.random())
                backoff = min(backoff*2,300)
    return 0


def stop(signum, _frame):
    global STOP
    STOP = True
    LOG.info('WORKER_SHUTDOWN signal=%d broker_orders=false',signum)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    raise SystemExit(main())
