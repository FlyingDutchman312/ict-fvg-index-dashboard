"""Read-only tastytrade DXLink market-data collector. No brokerage order functions."""
import json
import logging
import os
import time
import math
from fvg_engine import Bar, PaperFVG
from candle_validation import ValidatedCandleStream
from observation_store import ObservationStore
from candle_chart_store import CandleChartStore
from shadow_engine import ShadowEngine
from paper_simulator import PaperSimulator, paper_activation_enabled
from paper_diagnostics import PaperRejectionJournal
from signal_intelligence import SignalEvidenceLedger
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
import websocket

API = "https://api.tastyworks.com"
UA = "fvg-market-monitor/2.0"
LOG = logging.getLogger("fvg")
SYMBOLS = {"SPY": "SPY", "QQQ": "QQQ"}
QUOTE_FIELDS = ["eventType", "eventSymbol", "bidPrice", "askPrice", "bidSize", "askSize", "bidTime", "askTime"]
TRADE_FIELDS = ["eventType", "eventSymbol", "price", "size", "dayVolume"]
CANDLE_FIELDS = ["eventType", "eventSymbol", "eventTime", "eventFlags", "index", "time", "sequence", "count", "open", "high", "low", "close", "volume"]


def need_vars():
    missing = [name for name in ("TASTYTRADE_CLIENT_SECRET", "TASTYTRADE_REFRESH_TOKEN") if not os.getenv(name)]
    if missing:
        raise RuntimeError("Missing Railway Variables: " + ", ".join(missing))
    if os.getenv("FVG_PAPER_ONLY", "true").lower() != "true" or os.getenv("FVG_LIVE_TRADING_ENABLED", "false").lower() != "false":
        raise RuntimeError("Safety guard: FVG_PAPER_ONLY must be true and FVG_LIVE_TRADING_ENABLED must be false")


def api_json(method, endpoint, *, token=None, params=None, payload=None):
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    if payload is not None:
        headers["Content-Type"] = "application/json"
    response = requests.request(method, API + endpoint, headers=headers, params=params, json=payload, timeout=25)
    if not response.ok:
        # Deliberately suppress response body, URL query, access tokens, account IDs.
        try:
            body = response.json()
            error = body.get("error_code") or body.get("error", {}) if isinstance(body, dict) else None
            if isinstance(error, dict):
                error = error.get("code")
            safe = str(error)[:60] if isinstance(error, (str, int)) and str(error).replace("_", "").replace("-", "").isalnum() else "unavailable"
        except (ValueError, TypeError):
            safe = "non_json"
        raise RuntimeError(f"tastytrade API {endpoint} HTTP {response.status_code}; error_code={safe}")
    return response.json()


def oauth_token():
    result = api_json("POST", "/oauth/token", payload={
        "grant_type": "refresh_token",
        "refresh_token": os.environ["TASTYTRADE_REFRESH_TOKEN"],
        "client_secret": os.environ["TASTYTRADE_CLIENT_SECRET"],
    })
    token = result.get("access_token")
    if not token:
        raise RuntimeError("OAuth response missing access_token")
    LOG.info("OAuth authentication successful (read-only grant)")
    return token


def choose_active_future(items, code):
    valid = [x for x in items if isinstance(x, dict) and x.get("product-code") == code and x.get("active") is not False and x.get("streamer-symbol")]
    if not valid:
        return None
    # Prefer exchange-designated active month rather than nearest expired contract.
    valid.sort(key=lambda x: (not bool(x.get("active-month")), x.get("expiration-date") or "9999-12-31"))
    return valid[0]


def discover_symbols(token):
    symbols = dict(SYMBOLS)
    for label in ("MES", "MNQ"):
        try:
            doc = api_json("GET", "/instruments/futures", token=token, params={
                "product-code[]": label,
                "only-active-futures": "true",
                "per-page": 100,
            })
            items = doc.get("data", {}).get("items", [])
            chosen = choose_active_future(items, label)
            if chosen:
                symbols[label] = chosen["streamer-symbol"]
                LOG.info("%s selected current contract %s, DXLink symbol %s", label, chosen.get("symbol", "?"), symbols[label])
            else:
                LOG.warning("%s: no active futures contract resolved; no subscription", label)
        except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
            LOG.warning("%s futures discovery unavailable: %s", label, str(exc).split(";")[0])
    return symbols


def quote_token(access_token):
    data = api_json("GET", "/api-quote-tokens", token=access_token).get("data", {})
    url = data.get("dxlink-url")
    tok = data.get("token")
    if not isinstance(url, str) or not isinstance(tok, str) or not tok:
        raise RuntimeError("DXLink quote token or URL missing")
    u = urlparse(url)
    if u.scheme != "wss" or not u.hostname or not (u.hostname == "dxfeed.com" or u.hostname.endswith(".dxfeed.com") or u.hostname.endswith(".tastyworks.com") or u.hostname.endswith(".tastytrade.com")):
        raise RuntimeError("Unexpected DXLink server hostname; refusing WebSocket connection")
    level = str(data.get("level", "unknown"))[:40]
    LOG.info("DXLink quote token acquired; entitlement level=%s (not proof of real-time quotes)", level)
    return url, tok


def send(ws, message):
    ws.send(json.dumps(message, separators=(",", ":")))


def feed_setup():
    return {
        "type": "FEED_SETUP", "channel": 3, "acceptAggregationPeriod": 0.1,
        "acceptDataFormat": "COMPACT",
        "acceptEventFields": {"Quote": QUOTE_FIELDS, "Trade": TRADE_FIELDS, "Candle": CANDLE_FIELDS},
    }


def normalize_quote_epoch_ms(value):
    """Normalize DXLink Quote bidTime/askTime in Unix seconds or milliseconds.

    Reject missing, bools, fractions, and ambiguous/out-of-range values instead
    of guessing. This function performs no order/trading actions.
    """
    if value is None or isinstance(value, bool):
        return None, "missing" if value is None else "invalid"
    try:
        if isinstance(value, str):
            value = value.strip()
            if not value or not value.isdecimal():
                return None, "missing" if not value else "invalid"
        number = int(value)
        if isinstance(value, float) and (not math.isfinite(value) or value != number):
            return None, "invalid"
    except (TypeError, ValueError, OverflowError):
        return None, "invalid"
    # Identify units from range, *not* by comparing with local wall-clock time.
    if 1_600_000_000 <= number <= 4_000_000_000:
        return number * 1000, "seconds"
    if 1_600_000_000_000 <= number <= 4_000_000_000_000:
        return number, "milliseconds"
    return None, "invalid"


def quote_age_diagnostic(bid_time, ask_time, received_ms, max_age_ms=10_000):
    """Classify freshness of BOTH quote sides, based on their last-change times.

    The bid and ask may not change in an otherwise live market. This check does
    NOT verify market-data entitlement, exchange delivery latency, or fillability.
    """
    bid_ms, bid_unit = normalize_quote_epoch_ms(bid_time)
    ask_ms, ask_unit = normalize_quote_epoch_ms(ask_time)
    if bid_unit == "missing" or ask_unit == "missing":
        return "missing_timestamp", None
    if bid_ms is None or ask_ms is None:
        return "invalid_timestamp", None
    ages = [received_ms - bid_ms, received_ms - ask_ms]
    if any(age < -5_000 for age in ages):
        return "future_timestamp", None
    oldest_age_ms = max(ages)
    if oldest_age_ms > max_age_ms:
        return "stale", oldest_age_ms
    return "fresh", oldest_age_ms


def parse_feed_data(message, reverse_symbols, counters, last_sample, engines=None, pending=None, start_ms=None, shadow=None, paper=None, chart_store=None):
    """Quote collection plus native exchange-sourced 1m candles; never candle from quotes."""
    engines = engines if engines is not None else {}
    pending = pending if pending is not None else {}
    shadow = shadow if shadow is not None else {}
    paper = paper if paper is not None else {}
    entries = message.get("data", [])
    current_type = None
    for item in entries:
        if isinstance(item, str):
            current_type = item
            continue
        if not isinstance(item, list) or not current_type:
            continue
        if current_type == "Quote" and len(item) >= len(QUOTE_FIELDS):
            row = dict(zip(QUOTE_FIELDS, item))
            label = reverse_symbols.get(str(row.get("eventSymbol")))
            if not label:
                continue
            bid, ask = row.get("bidPrice"), row.get("askPrice")
            try:
                bid, ask = float(bid), float(ask)
                if not (math.isfinite(bid) and math.isfinite(ask) and 0 < bid <= ask):
                    continue
            except (TypeError, ValueError):
                continue
            if label in engines:
                engines[label].update_quote(bid, ask)
            counters[label] = counters.get(label, 0) + 1
            received_ms = int(time.time() * 1000)
            bid_time, ask_time = row.get("bidTime"), row.get("askTime")
            freshness, max_age_ms = quote_age_diagnostic(bid_time, ask_time, received_ms)
            counters[label + "_quote_" + freshness] = counters.get(label + "_quote_" + freshness, 0) + 1
            bid_ms, bid_unit = normalize_quote_epoch_ms(bid_time)
            ask_ms, ask_unit = normalize_quote_epoch_ms(ask_time)
            for side, unit in (("bid", bid_unit), ("ask", ask_unit)):
                key = label + "_" + side + "_time_unit_" + unit
                counters[key] = counters.get(key, 0) + 1
            if not counters.get(label + "_quote_timestamp_sample_logged", 0):
                # One public market-timestamp diagnostic per symbol and connection.
                # Log ONLY two timestamp fields and local receipt time: never the
                # complete DXLink message, credentials, account data, or tokens.
                # Use repr for integer/string values only; avoid arbitrary objects.
                def safe_timestamp_sample(value):
                    if type(value) is int and len(str(value)) <= 24:
                        return str(value)
                    if type(value) is str and len(value) <= 24 and value.isdecimal():
                        return value
                    return "unavailable"

                LOG.info("QUOTE_TIMESTAMP_RAW_SAMPLE %s bidTime=%s askTime=%s received_ms=%d bid_unit=%s ask_unit=%s bid_type=%s ask_type=%s freshness=%s",
                         label, safe_timestamp_sample(bid_time), safe_timestamp_sample(ask_time), received_ms,
                         bid_unit, ask_unit, type(bid_time).__name__, type(ask_time).__name__, freshness)
                counters[label + "_quote_timestamp_sample_logged"] = 1
            if time.monotonic() - last_sample.get(label, 0) > 120:
                LOG.info("Quote %s bid=%.4f ask=%.4f spread=%.4f received_utc=%s freshness=%s oldest_side_age_ms=%s (timestamps are quote-side times; execution/entitlement NOT verified)",
                         label, bid, ask, ask-bid, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                         freshness, max_age_ms if max_age_ms is not None else "unavailable")
                last_sample[label] = time.monotonic()
        elif current_type == "Candle" and len(item) >= len(CANDLE_FIELDS):
            row = dict(zip(CANDLE_FIELDS, item))
            candle_symbol = str(row.get("eventSymbol", ""))
            if not candle_symbol.endswith("{=m}"):
                continue
            base = candle_symbol[:-4]
            label = reverse_symbols.get(base)
            if label not in engines:
                continue
            try:
                ts = int(row["time"])
                # dxFeed Candle.time is milliseconds. Reject unexpected units or misaligned bars.
                if not (1_600_000_000_000 <= ts <= 4_000_000_000_000) or ts % 60_000:
                    continue
                bar = Bar(ts, *(float(row[k]) for k in ("open", "high", "low", "close", "volume")))
                if not all(math.isfinite(v) for v in (bar.open,bar.high,bar.low,bar.close)):
                    continue
            except (TypeError, ValueError, KeyError):
                continue
            counters[label + "_candles"] = counters.get(label + "_candles", 0) + 1
            validator = pending.setdefault(label, ValidatedCandleStream(label, start_ms or int(time.time()*1000)))
            closed, reason = validator.ingest(bar, received_ms=int(time.time()*1000), flags=row.get("eventFlags"))
            for complete, live in closed:
                if chart_store is not None:
                    chart_store.record(label, complete, live)
                # Never permit simulated execution in Phase 4. FVG detection only.
                engines[label].on_closed_bar(complete, live=live)
                if label in shadow:
                    shadow[label].on_bar(complete, live=live, engine=engines[label])
                if label in paper:
                    paper[label].on_bar(complete, live=live, engine=engines[label])
            if reason == "rejected":
                counters[label + "_rejected"] = counters.get(label + "_rejected", 0) + 1

def monitor_once(symbols, url, tok):
    reverse = {v: k for k, v in symbols.items()}
    counters = {k: 0 for k in symbols}
    samples = {}
    start_ms = int(time.time()*1000)
    paper_requested = os.environ.get("FVG_ENABLE_PAPER_SIM", "false").lower() == "true"
    if paper_requested:
        LOG.warning("Phase 4 validation mode overrides FVG_ENABLE_PAPER_SIM=true: paper entries disabled")
    paper_enabled = False
    fee = float(os.environ.get("FVG_EST_FEE_PER_SIDE_USD", "1.50"))
    slip = int(os.environ.get("FVG_EST_SLIPPAGE_TICKS", "1"))
    risk = float(os.environ.get("FVG_MAX_EST_RISK_USD", "30"))
    store = ObservationStore()
    # Phase 12 research evidence is non-blocking and never influences entry decisions.
    evidence_ledger = SignalEvidenceLedger()
    def sink_for(label):
        def save(event):
            if event.get("event") == "FVG_DETECTED":
                multiplier, tick = (5.0, 0.25) if label == "MES" else (2.0, 0.25)
                store.record_gap(event, contract_symbol=symbols[label],
                                 fee_side_usd=fee, slip_ticks=slip, tick_size=tick, multiplier=multiplier)
        return save
    engines = {label: PaperFVG(label, enabled=paper_enabled, fee_side=fee,
                                slip_ticks=slip, risk_cap=risk,
                                log_path=os.environ.get("FVG_EVENT_LOG_PATH") or None,
                                event_sink=sink_for(label))
               for label in ("MES", "MNQ") if label in symbols}
    pending = {}
    # Phase 6B is always shadow-only. Requires a private SQL journal; if absent,
    # it fails closed without affecting Phase 5 observation recording.
    shadow = {label: ShadowEngine(label, symbols[label],
                                  fee_side=fee, slip_ticks=slip,
                                  max_risk_usd=float(os.environ.get("FVG_SHADOW_MAX_RISK_USD", "30")),
                                  daily_loss_usd=float(os.environ.get("FVG_SHADOW_DAILY_LOSS_USD", "50")),
                                  max_trades_day=int(os.environ.get("FVG_SHADOW_MAX_TRADES_PER_DAY", "5")),
                                   evidence_sink=evidence_ledger.submit)
              for label in engines}
    # Phase 16: separate internal paper journal. FALSE by default and never calls broker APIs.
    paper = {}
    if paper_activation_enabled():
        rejection_journal = PaperRejectionJournal()
        paper = {label: PaperSimulator(label, symbols[label], fee_side=fee,
                                      slip_ticks=slip, rejection_sink=rejection_journal.submit)
                 for label in engines}
        LOG.info("PHASE16_PAPER_SIM enabled=True journals_ready=%s isolated_from_shadow=True broker_orders=False",
                 all(v.ready for v in paper.values()))
    else:
        LOG.info("PHASE16_PAPER_SIM enabled=False broker_orders=False")
    LOG.info("PHASE_6B_SHADOW journal=%s paper_entries=False live_orders=False quote_timestamp_verified=False",
             all(s.ready for s in shadow.values()))
    LOG.info("Phase 6A timestamp normalization: seconds/milliseconds supported; quote units logged; paper_entries=%s; live_orders=False", paper_enabled)
    LOG.info("Phase 5 observation journal: native 1m candles; forward gaps only; paper_entries=%s; live_orders=False", paper_enabled)
    LOG.info("ESTIMATED_COSTS fee_side_usd=%.2f slippage_ticks=%d max_risk_usd=%.2f; user inputs, NOT verified broker fees", fee, slip, risk)
    ws = websocket.create_connection(url, timeout=12, enable_multithread=True)
    ws.settimeout(5)
    chart_store = CandleChartStore(store, symbols)
    try:
        send(ws, {"type": "SETUP", "channel": 0, "version": "0.1-DXF-JS/0.3.0", "keepaliveTimeout": 60, "acceptKeepaliveTimeout": 60})
        authorized = False
        opened = False
        configured = False
        subscribed = False
        started = time.monotonic()
        last_keepalive = started
        last_report = started
        while True:
            now = time.monotonic()
            if now - started > 22 * 3600:  # Rotate token/socket before 24-hour expiry.
                LOG.info("Rotating DXLink connection before quote-token expiry")
                return
            if now - last_keepalive >= 25:
                send(ws, {"type": "KEEPALIVE", "channel": 0})
                last_keepalive = now
            if now - last_report >= 120:
                LOG.info("CANDLE_VALIDATION %s", json.dumps({
                    "streaming": subscribed,
                    "received": {k: counters.get(k + "_candles", 0) for k in engines},
                    "validation": {k: v.summary() for k, v in pending.items()},
                    "quote_freshness": {k: {status: counters.get(k + "_quote_" + status, 0)
                                            for status in ("fresh", "stale", "missing_timestamp", "invalid_timestamp", "future_timestamp")}
                                       for k in symbols},
                    "quote_time_units": {k: {side: {unit: counters.get(k + "_" + side + "_time_unit_" + unit, 0)
                                                          for unit in ("seconds", "milliseconds", "missing", "invalid")}
                                             for side in ("bid", "ask")}
                                         for k in symbols},
                    "shadow_scenarios": {k: v.summary() for k, v in shadow.items()},
                    "internal_paper_sim": {k: v.summary() for k, v in paper.items()},
                    "paper_enabled": paper_enabled,
                    "orders_enabled": False,
                }, separators=(",", ":")))
                last_report = now
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not raw:
                raise RuntimeError("DXLink socket closed")
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            typ = msg.get("type")
            if typ == "AUTH_STATE":
                state = msg.get("state")
                if state == "UNAUTHORIZED":
                    send(ws, {"type": "AUTH", "channel": 0, "token": tok})
                elif state == "AUTHORIZED" and not authorized:
                    authorized = True
                    LOG.info("DXLink authentication authorized")
                    send(ws, {"type": "CHANNEL_REQUEST", "channel": 3, "service": "FEED", "parameters": {"contract": "AUTO"}})
            elif typ == "CHANNEL_OPENED" and msg.get("channel") == 3 and not opened:
                opened = True
                send(ws, feed_setup())
            elif typ == "FEED_CONFIG" and msg.get("channel") == 3 and not configured:
                configured = True
                add = [{"type": "Quote", "symbol": s} for s in symbols.values()]
                add += [{"type": "Candle", "symbol": s + "{=m}",
                         "fromTime": (start_ms // 1000) - 90*60}
                        for label, s in symbols.items() if label in engines]
                send(ws, {"type": "FEED_SUBSCRIPTION", "channel": 3, "reset": True, "add": add})
                subscribed = True
                LOG.info("DXLink subscriptions requested for %s; waiting for events", ", ".join(symbols))
            elif typ == "FEED_DATA" and msg.get("channel") == 3:
                parse_feed_data(msg, reverse, counters, samples, engines, pending, start_ms, shadow, paper, chart_store)
            elif typ in ("ERROR", "CHANNEL_CLOSED", "AUTH_STATE"):
                LOG.warning("DXLink state event: %s (no message body logged)", typ)
    finally:
        chart_store.close()
        ws.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    need_vars()
    LOG.info("FVG market-data collector started | read-only | paper-only | no order execution")
    delay = 5
    while True:
        try:
            access = oauth_token()
            symbols = discover_symbols(access)
            url, token = quote_token(access)
            monitor_once(symbols, url, token)
            delay = 5
        except (requests.RequestException, websocket.WebSocketException, RuntimeError, ValueError, OSError) as exc:
            # API exceptions are sanitized in api_json; never output token or request bodies.
            LOG.error("Collector interrupted: %s", str(exc)[:160])
            LOG.info("Next attempt after %s seconds; no trading enabled", delay)
            time.sleep(delay)
            delay = min(delay * 2, 900)


if __name__ == "__main__":
    main()
