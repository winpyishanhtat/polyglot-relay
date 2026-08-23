import threading
import time
import unittest

from gemini_rate_gate import SlidingWindowLimiter


class SlidingWindowLimiterTests(unittest.TestCase):
    """A short window_seconds keeps this suite fast without faking time.monotonic."""

    def test_first_n_calls_within_limit_return_immediately(self):
        limiter = SlidingWindowLimiter(limit=3, window_seconds=5.0)
        start = time.monotonic()
        for _ in range(3):
            waited, _in_window = limiter.acquire()
            self.assertEqual(waited, 0.0)
        self.assertLess(time.monotonic() - start, 0.2)

    def test_call_over_the_limit_blocks_until_the_window_elapses(self):
        limiter = SlidingWindowLimiter(limit=1, window_seconds=0.3)
        limiter.acquire()
        start = time.monotonic()
        waited, in_window = limiter.acquire()
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.25)
        self.assertGreater(waited, 0.0)
        self.assertEqual(in_window, 1)

    def test_concurrent_callers_never_exceed_the_limit_in_one_window(self):
        limiter = SlidingWindowLimiter(limit=2, window_seconds=0.4)
        release_times: list[float] = []
        lock = threading.Lock()

        def worker():
            limiter.acquire()
            with lock:
                release_times.append(time.monotonic())

        threads = [threading.Thread(target=worker) for _ in range(6)]
        start = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        # 6 callers, 2 slots per 0.4s window: the 5th/6th callers can only
        # be released after roughly two window refills (~0.8s), not all at
        # once immediately.
        self.assertEqual(len(release_times), 6)
        first_two = sorted(release_times)[:2]
        last_two = sorted(release_times)[-2:]
        self.assertGreater(min(last_two) - start, 0.35)
        self.assertLess(max(first_two) - start, 0.35)


if __name__ == "__main__":
    unittest.main()
