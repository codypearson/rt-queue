"""Tests for single-flight caching."""

from __future__ import annotations

import threading
import unittest

from rt_queue.single_flight import SingleFlightCache


class SingleFlightCacheTests(unittest.TestCase):
    """Concurrent callers share one fetch."""

    def test_single_fetch_for_parallel_waiters(self) -> None:
        """Two threads waiting on the same key produce one fetch call."""
        cache: SingleFlightCache[str, int] = SingleFlightCache()
        call_count = 0
        lock = threading.Lock()

        def fetch() -> int:
            nonlocal call_count
            with lock:
                call_count += 1
            return 42

        results: list[int] = []

        def worker() -> None:
            results.append(cache.get_or_fetch("ENG-1", fetch))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(call_count, 1)
        self.assertEqual(results, [42, 42])
