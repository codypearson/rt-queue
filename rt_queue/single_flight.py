"""Thread-safe single-flight cache for idempotent HTTP-backed lookups."""

from __future__ import annotations

import threading
from typing import Callable, Generic, Hashable, TypeVar

KeyT = TypeVar("KeyT", bound=Hashable)
ValueT = TypeVar("ValueT")


class SingleFlightCache(Generic[KeyT, ValueT]):
    """
    Cache values by key so concurrent waiters share one in-flight fetch.

    The fetch callable runs under the cache lock; callers must not re-enter
    the same cache from inside fetch for the same key.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict[KeyT, ValueT] = {}
        self._in_flight: dict[KeyT, threading.Event] = {}

    def get_or_fetch(self, key: KeyT, fetch: Callable[[], ValueT]) -> ValueT:
        """Return a cached value, or compute it once per key."""
        with self._lock:
            if key in self._values:
                return self._values[key]
            event = self._in_flight.get(key)
            if event is None:
                event = threading.Event()
                self._in_flight[key] = event
                is_leader = True
            else:
                is_leader = False

        if not is_leader:
            event.wait()
            with self._lock:
                return self._values[key]

        try:
            value = fetch()
        except Exception:
            with self._lock:
                self._in_flight.pop(key, None)
                event.set()
            raise

        with self._lock:
            self._values[key] = value
            self._in_flight.pop(key, None)
            event.set()
        return value
