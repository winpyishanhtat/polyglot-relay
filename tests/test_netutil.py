"""Connections that never wait on DNS for a host reached before (a stopped container's name takes ~4 s to fail
in Docker), and never mistake "the name stopped resolving" for "the peer is down"."""

import ipaddress
import socket
import threading
import time
import unittest
from unittest import mock

import hermetic  # noqa: F401  (drops real API keys so no test reaches the network)

from common import config, discovery, netutil
from common.protocol import encode, recv_one


class FakeDns:
    """Stands in for socket.getaddrinfo: per-host delay, answer or failure, and a log of NAME lookups."""

    def __init__(self, answers=None, delays=None, failures=()):
        self.answers, self.delays, self.failures = answers or {}, delays or {}, set(failures)
        self.calls = []
        self._lock = threading.Lock()

    def __call__(self, host, port, *args, **kwargs):
        with self._lock:
            try:
                ipaddress.ip_address(host)       # create_connection looks up the numeric address too; not a name lookup
            except ValueError:
                self.calls.append(host)
        time.sleep(self.delays.get(host, 0))
        if host in self.failures:
            raise socket.gaierror(-2, "Name or service not known")
        # create_connection calls getaddrinfo itself with the real port, so hand that port back
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (self.answers.get(host, host if host[0].isdigit() else "127.0.0.1"), port or 0))]


def wait_for(condition, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class FirstLookupTests(unittest.TestCase):
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

    def test_a_good_name_resolves_once_and_is_then_remembered(self):
        dns = FakeDns(answers={"broker-1": "172.20.0.5"})
        with self.patched(dns):
            for _ in range(20):
                self.assertEqual(netutil.resolve("broker-1", 0.5), "172.20.0.5")
        self.assertEqual(dns.calls, ["broker-1"])

    def test_a_slow_first_lookup_costs_the_deadline_not_the_full_stall(self):
        dns = FakeDns(delays={"broker-3": 3.0})
        with self.patched(dns):
            started = time.monotonic()
            with self.assertRaises(TimeoutError):
                netutil.resolve("broker-3", 0.2)
            self.assertLess(time.monotonic() - started, 1.0)

    def test_after_a_failed_first_lookup_further_attempts_fail_at_once_for_a_moment(self):
        dns = FakeDns(delays={"broker-3": 3.0})
        with self.patched(dns):
            with self.assertRaises(TimeoutError):
                netutil.resolve("broker-3", 0.2)
            started = time.monotonic()
            for _ in range(20):
                with self.assertRaises(ConnectionError):
                    netutil.resolve("broker-3", 0.2)
            self.assertLess(time.monotonic() - started, 0.1)
        self.assertEqual(dns.calls, ["broker-3"])

    def test_a_name_that_does_not_exist_is_remembered_only_briefly(self):
        dns = FakeDns(failures=["broker-2"])
        with self.patched(dns), mock.patch.object(netutil, "NEGATIVE_TTL", 0.1):
            with self.assertRaises(socket.gaierror):
                netutil.resolve("broker-2", 0.5)
            with self.assertRaises(ConnectionError):
                netutil.resolve("broker-2", 0.5)
            time.sleep(0.15)
            dns.failures.clear()
            dns.answers["broker-2"] = "172.20.0.9"
            self.assertEqual(netutil.resolve("broker-2", 0.5), "172.20.0.9")

    def test_clear_cache_forgets_everything(self):
        dns = FakeDns(answers={"a": "172.20.0.1"})
        with self.patched(dns):
            netutil.resolve("a", 0.5)
            netutil.clear_cache()
            netutil.resolve("a", 0.5)
        self.assertEqual(dns.calls, ["a", "a"])


class AKnownHostThatStopsResolvingTests(unittest.TestCase):
    """The split-brain trigger: a broker restarts, its name briefly fails, and a probe wrongly sees it as down."""

    def setUp(self):
        netutil.clear_cache()
        self.addCleanup(netutil.clear_cache)

    def test_a_host_that_resolved_before_keeps_its_last_address_when_the_name_stops_resolving(self):
        dns = FakeDns(answers={"broker-3": "172.20.0.7"})
        with mock.patch.object(socket, "getaddrinfo", dns):
            self.assertEqual(netutil.resolve("broker-3", 0.5), "172.20.0.7")
            dns.failures.add("broker-3")                          # the container stopped: its name no longer resolves
            dns.delays["broker-3"] = 4.0                          # and a lookup would now stall for 4 s
            started = time.monotonic()
            for _ in range(50):
                self.assertEqual(netutil.resolve("broker-3", 0.5), "172.20.0.7")
            self.assertLess(time.monotonic() - started, 0.2)       # never waited, never raised

    def test_a_refused_connection_to_a_known_address_fails_fast_and_a_restart_is_found_straight_away(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        port = server.getsockname()[1]
        self.addCleanup(server.close)
        dns = FakeDns(answers={"broker-3": "127.0.0.1"})
        with mock.patch.object(socket, "getaddrinfo", dns):
            netutil.connect("broker-3", port, 1.0).close()          # first contact: resolved and remembered
            dns.failures.add("broker-3")                            # the name stops resolving (container down)
            dns.delays["broker-3"] = 4.0
            server.close()                                          # ... and nothing is listening
            started = time.monotonic()
            with self.assertRaises(OSError):
                netutil.connect("broker-3", port, 1.0)
            self.assertLess(time.monotonic() - started, 3.0)       # not the 4 s lookup stall (Windows itself takes ~1 s to refuse)
            restarted = socket.socket()
            restarted.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            restarted.bind(("127.0.0.1", port))
            restarted.listen(4)
            self.addCleanup(restarted.close)
            netutil.connect("broker-3", port, 1.0).close()         # the container is back: no lookup needed

    def test_an_address_that_changed_is_picked_up_in_the_background(self):
        dns = FakeDns(answers={"svc": "172.20.0.1"})
        with mock.patch.object(socket, "getaddrinfo", dns), mock.patch.object(netutil, "REFRESH_AFTER", 0.05), \
                mock.patch.object(netutil, "REFRESH_BACKOFF", 0.0):
            self.assertEqual(netutil.resolve("svc", 0.5), "172.20.0.1")
            dns.answers["svc"] = "172.20.0.2"
            time.sleep(0.08)
            started = time.monotonic()
            self.assertEqual(netutil.resolve("svc", 0.5), "172.20.0.1")      # this caller still gets the known one, instantly
            self.assertLess(time.monotonic() - started, 0.1)
            self.assertTrue(wait_for(lambda: netutil.resolve("svc", 0.5) == "172.20.0.2"))

    def test_a_failing_background_refresh_keeps_the_old_address(self):
        dns = FakeDns(answers={"svc": "172.20.0.1"})
        with mock.patch.object(socket, "getaddrinfo", dns), mock.patch.object(netutil, "REFRESH_AFTER", 0.05), \
                mock.patch.object(netutil, "REFRESH_BACKOFF", 0.0):
            netutil.resolve("svc", 0.5)
            dns.failures.add("svc")
            time.sleep(0.08)
            netutil.resolve("svc", 0.5)
            self.assertTrue(wait_for(lambda: len(dns.calls) >= 2))
            time.sleep(0.05)
            self.assertEqual(netutil.resolve("svc", 0.5), "172.20.0.1")

    def test_background_refreshes_are_throttled(self):
        dns = FakeDns(answers={"svc": "172.20.0.1"}, delays={})
        with mock.patch.object(socket, "getaddrinfo", dns), mock.patch.object(netutil, "REFRESH_AFTER", 0.0), \
                mock.patch.object(netutil, "REFRESH_BACKOFF", 5.0):
            netutil.resolve("svc", 0.5)
            for _ in range(200):
                netutil.resolve("svc", 0.5)
            time.sleep(0.1)
        self.assertLessEqual(len(dns.calls), 2)                 # the first lookup plus at most one refresh

    def test_a_slow_background_refresh_does_not_pile_up(self):
        dns = FakeDns(answers={"svc": "172.20.0.1"})
        with mock.patch.object(socket, "getaddrinfo", dns), mock.patch.object(netutil, "REFRESH_AFTER", 0.0), \
                mock.patch.object(netutil, "REFRESH_BACKOFF", 0.0):
            netutil.resolve("svc", 0.5)
            dns.delays["svc"] = 0.5
            for _ in range(100):
                netutil.resolve("svc", 0.5)
            time.sleep(0.7)
        self.assertLessEqual(len(dns.calls), 3)                 # one refresh in flight at a time


class ConnectTests(unittest.TestCase):
    def setUp(self):
        netutil.clear_cache()
        self.addCleanup(netutil.clear_cache)
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(64)
        self.port = self.server.getsockname()[1]
        self.addCleanup(self.server.close)

    def test_connects_through_the_resolved_address(self):
        dns = FakeDns(answers={"some-service": "127.0.0.1"})
        with mock.patch.object(socket, "getaddrinfo", dns):
            netutil.connect("some-service", self.port, 1.0).close()
        conn, _ = self.server.accept()
        conn.close()

    def test_numeric_hosts_connect_without_any_lookup(self):
        dns = FakeDns()
        with mock.patch.object(socket, "getaddrinfo", dns):
            netutil.connect("127.0.0.1", self.port, 1.0).close()
        self.assertEqual(dns.calls, [])

    def test_an_unresolvable_name_raises_an_os_error_so_callers_treat_it_like_any_dead_peer(self):
        with mock.patch.object(socket, "getaddrinfo", FakeDns(failures=["gone"])):
            with self.assertRaises(OSError):
                netutil.connect("gone", 7001, 0.5)

    def test_many_connections_cost_one_lookup(self):
        dns = FakeDns(answers={"svc": "127.0.0.1"})
        with mock.patch.object(socket, "getaddrinfo", dns):
            for _ in range(15):
                netutil.connect("svc", self.port, 1.0).close()
        self.assertEqual(dns.calls, ["svc"])


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

    def test_finding_the_leader_does_not_wait_out_a_never_resolved_brokers_lookup(self):
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
        self.assertLess(second, 0.3)        # the failed first lookup is remembered for a moment

    def test_a_broker_that_was_reached_before_and_is_now_down_costs_nothing_to_skip(self):
        dns = FakeDns(answers={"was-up": "127.0.0.1"})
        brokers = [{"id": 1, "host": "was-up", "port": 1}, {"id": 2, "host": "127.0.0.1", "port": self.live.getsockname()[1]}]
        with mock.patch.object(socket, "getaddrinfo", dns), mock.patch.object(config, "BROKERS", brokers), \
                mock.patch.object(config, "CONNECT_TIMEOUT", 0.3):
            netutil.resolve("was-up", 0.3)               # reached before
            dns.failures.add("was-up")
            dns.delays["was-up"] = 4.0                   # now its name stalls
            started = time.monotonic()
            for _ in range(3):
                sock, _ack = discovery.connect_to_leader({"type": "register", "role": "worker"})
                sock.close()
            self.assertLess(time.monotonic() - started, 3.0)       # three 4 s lookup stalls would take 12 s

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
