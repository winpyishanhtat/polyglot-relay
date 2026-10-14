"""Connections with a deadline on the DNS lookup (a stopped container's name takes ~4 s to fail in Docker)."""

import ipaddress
import socket
import threading
import time
import unittest

import hermetic  # noqa: F401  (drops real API keys so no test reaches the network)
from unittest import mock

from common import config, discovery, netutil
from common.protocol import encode, recv_one


class FakeDns:
    """Stands in for socket.getaddrinfo: per-host delay, answer or failure, and a call log."""

    def __init__(self, answers=None, delays=None, failures=()):
        self.answers, self.delays, self.failures = answers or {}, delays or {}, set(failures)
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, host, port, *args, **kwargs):
        with self._lock:
            try:
                ipaddress.ip_address(host)       # create_connection looks up the numeric address too; that is not a name lookup
            except ValueError:
                self.calls.append(host)
        time.sleep(self.delays.get(host, 0))
        if host in self.failures:
            raise socket.gaierror(-2, "Name or service not known")
        # create_connection calls getaddrinfo itself with the real port, so hand that port back
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (self.answers.get(host, host if host[0].isdigit() else "127.0.0.1"), port or 0))]


class ResolveTests(unittest.TestCase):
    def setUp(self):
        netutil.clear_cache()
        self.addCleanup(netutil.clear_cache)

    def patched(self, dns):
        return mock.patch.object(socket, "getaddrinfo", dns)

    def test_numeric_addresses_and_localhost_skip_the_lookup(self):
        dns = FakeDns()
        with self.patched(dns):
            for host in ("127.0.0.1", "10.2.3.4", "::1", "localhost"):
                self.assertEqual(netutil.resolve(host, 0.5), host)
        self.assertEqual(dns.calls, [])

    def test_a_good_name_resolves_and_is_remembered(self):
        dns = FakeDns(answers={"broker-1": "172.20.0.5"})
        with self.patched(dns):
            self.assertEqual(netutil.resolve("broker-1", 0.5), "172.20.0.5")
            self.assertEqual(netutil.resolve("broker-1", 0.5), "172.20.0.5")
        self.assertEqual(dns.calls, ["broker-1"])         # the second answer came from the cache

    def test_a_slow_lookup_costs_the_deadline_not_the_full_stall(self):
        dns = FakeDns(delays={"broker-3": 3.0})
        with self.patched(dns):
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                netutil.resolve("broker-3", 0.2)
            self.assertLess(time.monotonic() - started, 1.0)       # the real lookup takes 3 s

    def test_after_a_failure_the_next_attempts_fail_immediately_without_a_new_lookup(self):
        dns = FakeDns(delays={"broker-3": 3.0})
        with self.patched(dns):
            with self.assertRaises(TimeoutError):
                netutil.resolve("broker-3", 0.2)
            started = time.monotonic()
            for _ in range(20):
                with self.assertRaises(ConnectionError):
                    netutil.resolve("broker-3", 0.2)
            self.assertLess(time.monotonic() - started, 0.1)
        self.assertEqual(dns.calls, ["broker-3"])                  # one lookup, however many probes

    def test_a_name_that_does_not_exist_is_also_remembered_briefly(self):
        dns = FakeDns(failures=["broker-2"])
        with self.patched(dns):
            with self.assertRaises(socket.gaierror):
                netutil.resolve("broker-2", 0.5)
            with self.assertRaises(ConnectionError):
                netutil.resolve("broker-2", 0.5)
        self.assertEqual(dns.calls, ["broker-2"])

    def test_the_failure_memory_expires_so_a_restarted_container_is_found_again(self):
        dns = FakeDns(failures=["broker-2"])
        with self.patched(dns), mock.patch.object(netutil, "NEGATIVE_TTL", 0.1):
            with self.assertRaises(socket.gaierror):
                netutil.resolve("broker-2", 0.5)
            time.sleep(0.15)
            dns.failures.clear()
            dns.answers["broker-2"] = "172.20.0.9"
            self.assertEqual(netutil.resolve("broker-2", 0.5), "172.20.0.9")

    def test_a_good_answer_replaces_an_earlier_failure_record(self):
        dns = FakeDns(failures=["broker-2"])
        with self.patched(dns), mock.patch.object(netutil, "NEGATIVE_TTL", 0.05):
            with self.assertRaises(socket.gaierror):
                netutil.resolve("broker-2", 0.5)
            time.sleep(0.08)
            dns.failures.clear()
            netutil.resolve("broker-2", 0.5)
            self.assertEqual(netutil.resolve("broker-2", 0.5), "127.0.0.1")     # no ConnectionError from a stale record

    def test_a_cached_answer_expires_too(self):
        dns = FakeDns(answers={"broker-1": "172.20.0.5"})
        with self.patched(dns), mock.patch.object(netutil, "POSITIVE_TTL", 0.05):
            netutil.resolve("broker-1", 0.5)
            time.sleep(0.08)
            dns.answers["broker-1"] = "172.20.0.6"
            self.assertEqual(netutil.resolve("broker-1", 0.5), "172.20.0.6")

    def test_clear_cache_forgets_everything(self):
        dns = FakeDns(failures=["broker-2"])
        with self.patched(dns):
            with self.assertRaises(socket.gaierror):
                netutil.resolve("broker-2", 0.5)
            netutil.clear_cache()
            with self.assertRaises(socket.gaierror):
                netutil.resolve("broker-2", 0.5)
        self.assertEqual(dns.calls, ["broker-2", "broker-2"])


class ConnectTests(unittest.TestCase):
    def setUp(self):
        netutil.clear_cache()
        self.addCleanup(netutil.clear_cache)
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.port = self.server.getsockname()[1]
        self.addCleanup(self.server.close)

    def test_connects_through_the_resolved_address(self):
        dns = FakeDns(answers={"some-service": "127.0.0.1"})
        with mock.patch.object(socket, "getaddrinfo", dns):
            sock = netutil.connect("some-service", self.port, 1.0)
        sock.close()
        conn, _ = self.server.accept()
        conn.close()

    def test_numeric_hosts_connect_without_any_lookup(self):
        dns = FakeDns()
        with mock.patch.object(socket, "getaddrinfo", dns):
            netutil.connect("127.0.0.1", self.port, 1.0).close()
        self.assertEqual(dns.calls, [])

    def test_a_refused_connection_forgets_the_cached_address(self):
        dns = FakeDns(answers={"svc": "127.0.0.1"})
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        dead_port = closed.getsockname()[1]
        closed.close()
        with mock.patch.object(socket, "getaddrinfo", dns):
            netutil.connect("svc", self.port, 1.0).close()          # caches svc -> 127.0.0.1
            with self.assertRaises(OSError):
                netutil.connect("svc", dead_port, 0.5)
            netutil.connect("svc", self.port, 1.0).close()          # looked up again, not trusted from the cache
        self.assertEqual(dns.calls, ["svc", "svc"])

    def test_an_unresolvable_name_raises_an_os_error_so_callers_treat_it_like_any_dead_peer(self):
        with mock.patch.object(socket, "getaddrinfo", FakeDns(failures=["gone"])):
            with self.assertRaises(OSError):
                netutil.connect("gone", 7001, 0.5)


class WalkingTheBrokerListTests(unittest.TestCase):
    """The case that froze the dashboard: a dead broker's name stalls, a live broker is next in line."""

    def setUp(self):
        netutil.clear_cache()
        self.addCleanup(netutil.clear_cache)
        self.live = socket.socket()
        self.live.bind(("127.0.0.1", 0))
        self.live.listen(8)
        self.live.settimeout(3)
        self.addCleanup(self.live.close)
        self.stop = threading.Event()
        self.addCleanup(self.stop.set)
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.live.accept()
            except (OSError, socket.timeout):
                continue
            try:
                message = recv_one(conn)
                if message["type"] == "register":
                    conn.sendall(encode({"type": "register_ack", "accepted": True, "leader_id": 2}))
                elif message["type"] == "discover_worker":
                    conn.sendall(encode({"type": "worker_info", "worker_id": "th-1", "host": "h", "port": 1}))
            except OSError:
                pass
            finally:
                conn.close()

    def brokers(self):
        port = self.live.getsockname()[1]
        return [{"id": 1, "host": "dead-broker", "port": 7001}, {"id": 2, "host": "127.0.0.1", "port": port}]

    def test_finding_the_leader_does_not_wait_out_a_dead_brokers_name_lookup(self):
        dns = FakeDns(delays={"dead-broker": 4.0}, failures=["dead-broker"])
        with mock.patch.object(socket, "getaddrinfo", dns), mock.patch.object(config, "BROKERS", self.brokers()), \
                mock.patch.object(config, "CONNECT_TIMEOUT", 0.3):
            started = time.monotonic()
            sock, ack = discovery.connect_to_leader({"type": "register", "role": "worker"})
            first = time.monotonic() - started
            sock.close()
            started = time.monotonic()
            sock, _ = discovery.connect_to_leader({"type": "register", "role": "worker"})
            second = time.monotonic() - started
            sock.close()
        self.assertTrue(ack["accepted"])
        self.assertLess(first, 1.5)         # was 4 s
        self.assertLess(second, 0.3)        # the dead name is remembered

    def test_asking_for_a_worker_endpoint_skips_a_dead_brokers_stall_too(self):
        dns = FakeDns(delays={"dead-broker": 4.0}, failures=["dead-broker"])
        with mock.patch.object(socket, "getaddrinfo", dns), mock.patch.object(config, "BROKERS", self.brokers()), \
                mock.patch.object(config, "CONNECT_TIMEOUT", 0.3):
            started = time.monotonic()
            info = discovery.discover_worker("th")
            elapsed = time.monotonic() - started
        self.assertEqual(info["worker_id"], "th-1")
        self.assertLess(elapsed, 1.5)


if __name__ == "__main__":
    unittest.main()
