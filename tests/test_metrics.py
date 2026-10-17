"""Metrics for Prometheus and Grafana: the counters themselves, what the brokers and workers report, the
/metrics page, and the Grafana dashboard that reads it."""

import json
import os
import re
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import hermetic  # noqa: F401  (drops real API keys so no test reaches the network)

import broker as broker_module
import metrics
import web_ui
from test_broker_server import FakeSocket, leader_broker, worker
from translator_worker import TranslatorWorker

ROOT = Path(__file__).resolve().parents[1]


class RegistryTests(unittest.TestCase):
    def test_counters_render_in_the_prometheus_text_format(self):
        registry = metrics.Registry()
        registry.describe("x_total", "Things")
        registry.inc("x_total", lang="th")
        registry.inc("x_total", 2, lang="th")
        registry.inc("x_total", lang="my")
        lines = registry.render()
        self.assertEqual(lines[:2], ["# HELP x_total Things", "# TYPE x_total counter"])
        self.assertIn('x_total{lang="th"} 3', lines)
        self.assertIn('x_total{lang="my"} 1', lines)

    def test_histograms_are_cumulative_with_sum_and_count(self):
        registry = metrics.Registry()
        for value in (0.4, 1.5, 70):
            registry.observe("d_seconds", value, buckets=(1.0, 2.0), lang="th")
        lines = registry.render()
        self.assertIn('d_seconds_bucket{lang="th",le="1"} 1', lines)
        self.assertIn('d_seconds_bucket{lang="th",le="2"} 2', lines)
        self.assertIn('d_seconds_bucket{lang="th",le="+Inf"} 3', lines)
        self.assertIn('d_seconds_sum{lang="th"} 71.9', lines)
        self.assertIn('d_seconds_count{lang="th"} 3', lines)

    def test_label_values_are_escaped(self):
        registry = metrics.Registry()
        registry.inc("x_total", room='a"b\\c\nd')
        self.assertIn('x_total{room="a\\"b\\\\c d"} 1', registry.render())

    def test_gauges_from_outside(self):
        lines = []
        metrics.gauge(lines, "g", "help", [({"a": "1"}, 2), ({}, 3)])
        self.assertEqual(lines, ["# HELP g help", "# TYPE g gauge", 'g{a="1"} 2', "g 3"])


class BrokerCounterTests(unittest.TestCase):
    def chunk(self, n=1):
        return {"type": "transcript_chunk", "chunk_id": f"c{n}", "text": "x", "targets": ["th"]}

    def test_routing_finishing_redelivering_and_failing_are_counted(self):
        broker = leader_broker()
        a, b = worker("th", 1, FakeSocket()), worker("th", 2, FakeSocket())
        broker.registry.add(a)
        broker.registry.add(b)
        broker.route_chunk(self.chunk(1))
        holder = a if a.assigned else b
        broker._on_translated(holder, {"type": "translated_chunk", "chunk_id": "c1", "lang": "th", "text": "t"})
        broker.route_chunk(self.chunk(2))
        holder = a if a.assigned else b
        broker._on_worker_gone(broker.registry.remove(holder.worker_id))       # c2 goes to the other worker
        counters = {(c["outcome"], c["lang"]): c["value"] for c in broker.telemetry()["counters"]}
        self.assertEqual(counters[("routed", "th")], 2)
        self.assertEqual(counters[("completed", "th")], 1)
        self.assertEqual(counters[("redelivered", "th")], 1)

    def test_the_telemetry_reports_waiting_and_pending_work_and_the_election(self):
        broker = leader_broker()
        broker.route_chunk(self.chunk(1))                                      # no worker: waits
        telemetry = broker.telemetry()
        self.assertEqual(telemetry["waiting"], {"th": 1})
        self.assertEqual(telemetry["pending"], 0)
        self.assertEqual(telemetry["election"], "bully")
        self.assertEqual(telemetry["broker_id"], 1)

    def test_a_workers_heartbeat_stats_are_kept_and_reported(self):
        broker = leader_broker()
        import socket as socket_module
        server_side, client_side = socket_module.socketpair()
        self.addCleanup(client_side.close)
        from common.protocol import encode, recv_one
        client_side.sendall(encode({"type": "register", "role": "worker", "lang": "th", "worker_id": "th-9",
                                    "host": "127.0.0.1", "port": 9}))
        threading.Thread(target=broker._handle_connection, args=(server_side, ("127.0.0.1", 1)), daemon=True).start()
        client_side.settimeout(2)
        recv_one(client_side)
        client_side.sendall(encode({"type": "heartbeat", "sender_id": "th-9", "role": "worker",
                                    "stats": {"translated": 5, "failed": 1, "cache_hits": 2, "junk": "x"}}))
        deadline = time.time() + 2
        while time.time() < deadline and not broker.telemetry()["members"][0]["stats"]:
            time.sleep(0.02)
        self.assertEqual(broker.telemetry()["members"][0]["stats"], {"translated": 5, "failed": 1, "cache_hits": 2})


class WorkerStatsTests(unittest.TestCase):
    def test_the_worker_counts_its_translations_failures_and_cache_hits(self):
        worker_ = TranslatorWorker("th", "127.0.0.1", 0, "th-stats", mock=True)
        worker_._send_broker = mock.Mock()
        worker_._process_chunk(mock.Mock(), {"chunk_id": "c1", "text": "hello"})
        worker_.translator.translate = mock.Mock(side_effect=RuntimeError("boom"))
        worker_._process_chunk(mock.Mock(), {"chunk_id": "c2", "text": "x"})
        worker_.translator.cache_hits = 4
        self.assertEqual(worker_.stats(), {"translated": 1, "failed": 1, "cache_hits": 4, "cache_entries": 0})


class MetricsPageTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"POLYGLOT_HISTORY_FILE": "", "POLYGLOT_VIEW_TOKEN": "", "POLYGLOT_ACCESS_TOKEN": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        registry = mock.patch.object(metrics, "REGISTRY", metrics.Registry())     # nothing left over from other tests
        registry.start()
        self.addCleanup(registry.stop)
        self.runtime = web_ui.DashboardRuntime()
        self.runtime.producer = mock.Mock()
        web_ui.DashboardHandler.runtime = self.runtime
        web_ui.DashboardHandler.limiter = web_ui.RequestRateLimiter()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web_ui.DashboardHandler)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.runtime.stop)
        brokers = [{"id": 1, "online": True, "leader": False}, {"id": 2, "online": False, "leader": False},
                   {"id": 3, "online": True, "leader": True,
                    "members": [{"worker_id": "th-1", "lang": "th", "queue": 2, "stats": {"translated": 7, "failed": 1, "cache_hits": 3}},
                                {"worker_id": "th-2", "lang": "th", "queue": 1, "stats": {}}],
                    "waiting": {"km": 4}, "counters": [{"outcome": "redelivered", "lang": "th", "value": 2}]}]
        patcher = mock.patch.object(self.runtime, "broker_status", return_value=brokers)
        patcher.start()
        self.addCleanup(patcher.stop)

    def scrape(self, headers=None):
        request = urllib.request.Request(self.base + "/metrics", headers=headers or {})
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.headers["Content-Type"], response.read().decode("utf-8")

    def test_the_page_reports_the_cluster(self):
        kind, body = self.scrape()
        self.assertTrue(kind.startswith("text/plain; version=0.0.4"))
        for expected in ('polyglot_broker_up{broker="2"} 0', 'polyglot_broker_is_leader{broker="3"} 1', "polyglot_leader_count 1",
                         'polyglot_workers{lang="th"} 2', 'polyglot_queue_depth{lang="th"} 3', 'polyglot_waiting_jobs{lang="km"} 4',
                         'polyglot_jobs_total{broker="3",lang="th",outcome="redelivered"} 2',
                         'polyglot_worker_translations_total{lang="th",worker="th-1"} 7',
                         'polyglot_translation_cache_hits_total{lang="th",worker="th-1"} 3', "polyglot_rooms 1"):
            self.assertIn(expected, body)

    def test_caption_delays_and_counts_are_measured_per_language(self):
        state = self.runtime.state
        state.record_original("c1", "hello", "en")
        state.history["c1"]["created_at"] = time.time() - 2.5
        state.add_caption({"lang": "th", "chunk_id": "c1", "text": "x", "translation_backend": "libretranslate", "worker_id": "th-1"})
        state.add_caption({"lang": "en", "chunk_id": "c1", "text": "hello", "translation_backend": "passthrough", "worker_id": "source"})
        _, body = self.scrape()
        self.assertRegex(body, r'polyglot_caption_delay_seconds_bucket\{lang="th",le="3"\} [1-9]')
        self.assertNotIn('polyglot_caption_delay_seconds_count{lang="en"}', body)        # the original is not a translation
        self.assertRegex(body, r'polyglot_captions_total\{backend="libretranslate",lang="th",room="main"\} [1-9]')
        self.assertRegex(body, r'polyglot_chunks_total\{room="main"\} [1-9]')

    def test_every_line_is_valid_prometheus_text(self):
        _, body = self.scrape()
        sample = re.compile(r'^[a-zA-Z_:][a-zA-Z0-9_:]*(\{[a-zA-Z_][a-zA-Z0-9_]*="(?:[^"\\]|\\.)*"(,[a-zA-Z_][a-zA-Z0-9_]*="(?:[^"\\]|\\.)*")*\})? -?[0-9.e+Inf]+$')
        for line in body.strip().splitlines():
            if line.startswith("#"):
                self.assertRegex(line, r"^# (HELP|TYPE) ")
            else:
                self.assertRegex(line, sample)

    def test_a_private_room_needs_the_token_and_prometheus_can_send_it(self):
        with mock.patch.dict(os.environ, {"POLYGLOT_VIEW_TOKEN": "look"}):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.scrape()
            self.assertEqual(caught.exception.code, 401)
            self.assertIn("polyglot_broker_up", self.scrape({"Authorization": "Bearer look"})[1])


class GrafanaDashboardTests(unittest.TestCase):
    def test_the_dashboard_only_uses_metrics_the_relay_exports(self):
        dashboard = json.loads((ROOT / "monitoring" / "grafana" / "dashboards" / "polyglot.json").read_text(encoding="utf-8"))
        exported = {"polyglot_leader_count", "polyglot_broker_is_leader", "polyglot_broker_up", "polyglot_workers", "polyglot_rooms",
                    "polyglot_event_streams", "polyglot_translation_cache_hits_total", "polyglot_worker_translations_total",
                    "polyglot_caption_delay_seconds_bucket", "polyglot_captions_total", "polyglot_queue_depth", "polyglot_waiting_jobs",
                    "polyglot_jobs_total", "polyglot_speech_calls_today", "polyglot_speech_quota_left"}
        used = set()
        for panel in dashboard["panels"]:
            self.assertTrue(panel["targets"], panel["title"])
            for target in panel["targets"]:
                used.update(re.findall(r"polyglot_[a-z_]+", target["expr"]))
        self.assertEqual(used - exported, set())
        self.assertEqual(dashboard["uid"], "polyglot-relay")

    def test_the_monitoring_files_are_wired_together(self):
        compose = (ROOT / "compose.monitoring.yaml").read_text(encoding="utf-8")
        self.assertIn("./monitoring/prometheus.yml:/etc/prometheus/prometheus.yml", compose)
        self.assertIn("127.0.0.1:3000:3000", compose)
        self.assertIn("ui:8080", (ROOT / "monitoring" / "prometheus.yml").read_text(encoding="utf-8"))
        self.assertIn("uid: prometheus", (ROOT / "monitoring" / "grafana" / "provisioning" / "datasources" / "prometheus.yml").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
