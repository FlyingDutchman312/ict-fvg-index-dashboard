"""Publish validated candles without blocking the DXLink receive loop."""

import logging
from queue import Empty, Full, Queue
from threading import Thread

import requests

LOG = logging.getLogger(__name__)


class CandleChartStore:
    def __init__(self, store, symbols):
        self.store = store
        self.symbols = symbols
        self.queue = Queue(maxsize=500)
        self.dropped = 0
        self.thread = None
        if store.enabled:
            self.thread = Thread(target=self._publish, daemon=True)
            self.thread.start()

    def record(self, instrument, bar, live):
        if self.thread is None:
            return
        row = dict(instrument=instrument, contract_symbol=self.symbols[instrument],
                   start_ms=bar.start_ms, open=bar.open, high=bar.high, low=bar.low,
                   close=bar.close, live=live)
        try:
            self.queue.put_nowait(row)
        except Full:
            self.dropped += 1
            if self.dropped % 100 == 1:
                LOG.warning("Chart candle queue full; dropped=%s", self.dropped)

    def close(self):
        if self.thread is None:
            return
        # Signal the publisher to stop after draining the bounded queue.
        while True:
            try:
                self.queue.put_nowait(None)
                break
            except Full:
                try:
                    self.queue.get_nowait()
                except Empty:
                    pass

    def _publish(self):

        headers = {
            "apikey": self.store.key,
            "Prefer": "resolution=ignore-duplicates,return=minimal",
        }
        if not self.store.key.startswith(("sb_secret_", "sb_publishable_")):
            headers["Authorization"] = "Bearer " + self.store.key
        endpoint = self.store.url + "/rest/v1/fvg_chart_candles"
        with requests.Session() as session:
            while True:
                row = self.queue.get()
                if row is None:
                    return
                batch = [row]
                stop = False
                while len(batch) < 100:
                    try:
                        row = self.queue.get_nowait()
                    except Empty:
                        break
                    if row is None:
                        stop = True
                        break
                    batch.append(row)
                try:
                    response = session.post(endpoint, headers=headers, json=batch,
                                            timeout=5, allow_redirects=False)
                    if response.status_code not in (200, 201, 204):
                        LOG.warning("Chart candle write failed (HTTP %s)", response.status_code)
                except requests.RequestException:
                    LOG.warning("Chart candle write failed (network error)")
                if stop:
                    return
