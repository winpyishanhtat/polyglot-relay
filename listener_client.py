"""Reconnecting terminal caption client."""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import socket
import time

from common import config
from common import netutil
from common.discovery import discover_worker
from common.protocol import ProtocolError, decode_stream

LOG = logging.getLogger("polyglot.listener")


def listen(lang: str) -> None:
    while True:
        worker = discover_worker(lang)
        if not worker:
            LOG.warning("No %s worker available; retrying", lang)
            time.sleep(config.RECONNECT_DELAY)
            continue
        try:
            with netutil.connect(worker["host"], worker["port"], config.CONNECT_TIMEOUT) as sock:
                sock.settimeout(None)
                LOG.info("Connected to worker %s", worker["worker_id"])
                for message in decode_stream(sock):
                    if message["type"] == "translated_chunk":
                        stamp = dt.datetime.fromtimestamp(float(message.get("timestamp", time.time()))).strftime("%H:%M:%S")
                        print(f"[{stamp}] {message['text']}", flush=True)
            # A worker that closes the stream cleanly (restart, rebalance) used to be
            # re-dialled immediately, in a tight loop; wait like the error path does.
            LOG.warning("Worker closed the connection; finding another worker")
            time.sleep(config.RECONNECT_DELAY)
        except (OSError, ConnectionError, ProtocolError) as exc:
            LOG.warning("Listener connection lost (%s); finding another worker", exc)
            time.sleep(config.RECONNECT_DELAY)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lang", choices=config.TARGET_LANGUAGES, required=True)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper()), format="%(asctime)s %(levelname)s: %(message)s")
    try:
        listen(args.lang)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
