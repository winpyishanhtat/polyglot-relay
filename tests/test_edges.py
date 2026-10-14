"""Edge cases the main suites leave out: retry limits, shutdown, command-line entry points and the pieces of
the dashboard that decide whether to use the shared English rendering."""

import logging
import sys
import threading
import time
import unittest
from unittest import mock

import hermetic  # noqa: F401  (drops real API keys so no test reaches the network)

import broker as broker_module
import listener_client
import web_ui
from test_broker_server import FakeSocket, leader_broker, wait_for, worker
from test_delivery import chunk, frames, two_workers


class RetryLimitTests(unittest.TestCase):
    def test_a_job_a_lost_worker_held_is_failed_once_the_retry_limit_is_reached(self):
        broker = leader_broker()
        (wa, a), (wb, b) = two_workers(broker)
        broker.route_chunk(chunk(1))
        holder = wa if wa.assigned else wb
        broker.pending[("c1", "th")]["attempts"] = broker_module.MAX_ATTEMPTS
        broker._on_worker_gone(holder)
        self.assertEqual(broker.jobs[("c1", "th")]["status"], "failed")
        self.assertIn("retry limit", broker.jobs[("c1", "th")]["detail"])
        self.assertNotIn(("c1", "th"), broker.pending)
        self.assertEqual(len(frames(a)) + len(frames(b)), 1)          # never sent a third time

    def test_a_lost_worker_holding_a_job_nobody_tracks_causes_no_delivery(self):
        broker = leader_broker()
        (wa, a), (wb, b) = two_workers(broker)
        wa.assigned["ghost"] = {"type": "transcript_chunk", "chunk_id": "ghost", "text": "x", "targets": ["th"]}
        broker._on_worker_gone(wa)
        self.assertEqual(frames(a) + frames(b), [])
        self.assertEqual(wa.assigned, {})

    def test_a_stuck_job_is_failed_once_the_retry_limit_is_reached(self):
        broker = leader_broker()
        (wa, a), (wb, b) = two_workers(broker)
        broker.route_chunk(chunk(1))
        broker.pending[("c1", "th")]["attempts"] = broker_module.MAX_ATTEMPTS
        with mock.patch.object(broker_module, "JOB_TIMEOUT", 0.05):
            time.sleep(0.1)
            broker._redeliver_stuck()
        self.assertEqual(broker.jobs[("c1", "th")]["status"], "failed")
        self.assertEqual(broker.jobs[("c1", "th")]["detail"], "No result within the time limit")
        self.assertEqual(len(frames(a)) + len(frames(b)), 1)

    def test_a_stuck_job_that_was_replaced_while_being_checked_is_left_alone(self):
        broker = leader_broker()
        (wa, a), (wb, b) = two_workers(broker)
        broker.route_chunk(chunk(1))

        class Racy:
            """On the second time the lock is taken (the per-job check) another delivery replaces the entry."""
            def __init__(self, inner, hook):
                self.inner, self.hook, self.entries = inner, hook, 0

            def __enter__(self):
                self.entries += 1
                if self.entries == 2:
                    self.hook()
                return self.inner.__enter__()

            def __exit__(self, *exc):
                return self.inner.__exit__(*exc)

        broker._jobs_lock = Racy(broker._jobs_lock, lambda: broker.pending.update({("c1", "th"): {**broker.pending[("c1", "th")]}}))
        with mock.patch.object(broker_module, "JOB_TIMEOUT", 0.05):
            time.sleep(0.1)
            broker._redeliver_stuck()
        self.assertEqual(len(frames(a)) + len(frames(b)), 1)                # not delivered a second time
        self.assertIn(("c1", "th"), broker.pending)


class ReplicatorTests(unittest.TestCase):
    def test_a_failing_flush_is_logged_and_the_loop_carries_on(self):
        broker = leader_broker()
        calls = []

        def flush():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("disk on fire")
        with mock.patch.object(broker_module, "REPLICATION_INTERVAL", 0.01), \
                mock.patch.object(broker, "_replicate_flush", side_effect=flush), \
                self.assertLogs("polyglot.broker", level="ERROR") as logs:
            thread = threading.Thread(target=broker._replicator, daemon=True)
            thread.start()
            self.assertTrue(wait_for(lambda: len(calls) >= 3))
            broker.stop_event.set()
            thread.join(1)
        self.assertTrue(any("Replication flush failed" in line for line in logs.output))

    def test_a_standby_does_not_replicate(self):
        broker = leader_broker(1)
        broker.election.leader_id = 2
        with mock.patch.object(broker_module, "REPLICATION_INTERVAL", 0.01), \
                mock.patch.object(broker, "_replicate_flush") as flush:
            thread = threading.Thread(target=broker._replicator, daemon=True)
            thread.start()
            time.sleep(0.1)
            broker.stop_event.set()
            thread.join(1)
        flush.assert_not_called()


class BrokerLifecycleTests(unittest.TestCase):
    def test_shutdown_closes_the_server_and_every_client_and_is_safe_to_repeat(self):
        broker = leader_broker()
        server = mock.Mock()
        broker.server = server
        sock = FakeSocket()
        broker.registry.add(worker("th", 1, sock))
        broker.shutdown()
        server.close.assert_called_once()
        self.assertTrue(broker.stop_event.is_set())
        self.assertIsNone(broker.server)
        self.assertEqual(broker.registry.snapshot("th"), [])
        broker.shutdown()                                               # again: no error, nothing more to close
        server.close.assert_called_once()

    def test_shutdown_survives_a_server_that_will_not_close(self):
        broker = leader_broker()
        broker.server = mock.Mock(**{"close.side_effect": OSError("already closed")})
        broker.shutdown()
        self.assertIsNone(broker.server)

    def test_the_command_line_starts_the_requested_broker_and_stops_it_on_a_signal(self):
        fake = mock.Mock()
        handlers = {}
        with mock.patch.object(sys, "argv", ["broker.py", "--id", "2", "--log-level", "warning"]), \
                mock.patch.object(broker_module, "Broker", return_value=fake) as broker_class, \
                mock.patch.object(broker_module.signal, "signal", side_effect=lambda sig, fn: handlers.update({sig: fn})):
            broker_module.main()
        broker_class.assert_called_once_with(2)
        fake.serve_forever.assert_called_once()
        self.assertEqual(set(handlers), {broker_module.signal.SIGINT, broker_module.signal.SIGTERM})
        handlers[broker_module.signal.SIGTERM](None, None)
        fake.shutdown.assert_called_once()

    def test_the_command_line_needs_a_broker_id(self):
        with mock.patch.object(sys, "argv", ["broker.py"]), self.assertRaises(SystemExit) as caught:
            broker_module.main()
        self.assertEqual(caught.exception.code, 2)


class ListenerClientCommandLineTests(unittest.TestCase):
    def test_it_listens_in_the_requested_language(self):
        with mock.patch.object(sys, "argv", ["listener_client.py", "--lang", "km"]), \
                mock.patch.object(listener_client, "listen") as listen:
            listener_client.main()
        listen.assert_called_once_with("km")

    def test_control_c_ends_it_quietly(self):
        with mock.patch.object(sys, "argv", ["listener_client.py", "--lang", "th"]), \
                mock.patch.object(listener_client, "listen", side_effect=KeyboardInterrupt):
            listener_client.main()

    def test_an_unknown_language_is_refused(self):
        with mock.patch.object(sys, "argv", ["listener_client.py", "--lang", "xx"]), \
                mock.patch.object(listener_client, "listen") as listen, self.assertRaises(SystemExit) as caught:
            listener_client.main()
        self.assertEqual(caught.exception.code, 2)
        listen.assert_not_called()

    def test_a_language_is_required(self):
        with mock.patch.object(sys, "argv", ["listener_client.py"]), self.assertRaises(SystemExit):
            listener_client.main()


class SharedEnglishRenderingDecisionTests(unittest.TestCase):
    """The dashboard only translates to English once (and lets workers reuse it) when the English pool is real."""

    def runtime(self):
        runtime = web_ui.DashboardRuntime()
        runtime.producer = mock.Mock()
        self.addCleanup(runtime.stop)
        return runtime

    def status(self, *brokers):
        return mock.patch.object(web_ui.DashboardRuntime, "broker_status", return_value=list(brokers))

    def test_a_real_english_worker_on_the_leader_counts(self):
        runtime = self.runtime()
        with self.status({"online": True, "leader": True, "members": [{"lang": "en", "mode": "gemini"}]}):
            self.assertTrue(runtime._cluster_is_real())

    def test_only_mock_english_workers_do_not(self):
        runtime = self.runtime()
        with self.status({"online": True, "leader": True, "members": [{"lang": "en", "mode": "mock"}]}):
            self.assertFalse(runtime._cluster_is_real())

    def test_other_languages_and_standby_brokers_do_not_count(self):
        runtime = self.runtime()
        with self.status({"online": True, "leader": True, "members": [{"lang": "th", "mode": "gemini"}]},
                         {"online": True, "leader": False, "members": [{"lang": "en", "mode": "gemini"}]},
                         {"online": False, "leader": True, "members": [{"lang": "en", "mode": "gemini"}]}):
            self.assertFalse(runtime._cluster_is_real())

    def test_the_answer_is_cached_for_a_few_seconds_so_the_send_path_stays_fast(self):
        runtime = self.runtime()
        with self.status({"online": True, "leader": True, "members": [{"lang": "en", "mode": "gemini"}]}) as asked:
            self.assertTrue(runtime._cluster_is_real())
            self.assertTrue(runtime._cluster_is_real())
        self.assertEqual(asked.call_count, 1)

    def test_it_is_asked_again_once_the_cache_is_old(self):
        runtime = self.runtime()
        with self.status({"online": True, "leader": True, "members": [{"lang": "en", "mode": "gemini"}]}) as asked:
            runtime._cluster_is_real()
            runtime._pivot_cluster_checked = (time.monotonic() - 11, True)
            runtime._cluster_is_real()
        self.assertEqual(asked.call_count, 2)

    def test_a_cluster_that_cannot_be_reached_is_treated_as_not_real(self):
        runtime = self.runtime()
        with self.status():
            self.assertFalse(runtime._cluster_is_real())


class SecondStageFailureTests(unittest.TestCase):
    def test_a_failure_sending_the_pivot_stage_is_reported_on_the_dashboard_not_raised(self):
        runtime = web_ui.DashboardRuntime()
        runtime.producer = mock.Mock()
        self.addCleanup(runtime.stop)
        runtime.producer.send.side_effect = [None, RuntimeError("broker gone")]
        with mock.patch.object(runtime, "_pivot_applies", return_value=True), \
                mock.patch.object(runtime, "_pivot_to_english", return_value="Hello"), \
                mock.patch.object(web_ui, "PIVOT_SOURCES", {"my", "km", "lo"}), \
                self.assertLogs("polyglot.ui", level="ERROR"):
            runtime._send_in_two_stages("မင်္ဂလာပါ", "c1", "my")
            self.assertTrue(wait_for(lambda: any("partly failed" in e["message"] for e in runtime.state.snapshot()["activity"])))


if __name__ == "__main__":
    unittest.main()
