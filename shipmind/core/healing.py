"""Self-healing primitives: retry with exponential backoff, circuit breaker, and a heal log."""
from __future__ import annotations

import threading
import time
from typing import Callable, TypeVar

T = TypeVar("T")


def log_event(heal_log: list | None, kind: str, detail: str) -> None:
    if heal_log is not None:
        heal_log.append({"ts": round(time.time(), 3), "kind": kind, "detail": detail})


class CircuitBreaker:
    """Stops hammering a failing dependency; half-opens after a cooldown to probe recovery."""

    def __init__(self, threshold: int = 3, cooldown_s: float = 5.0):
        self.threshold, self.cooldown_s = threshold, cooldown_s
        self._fails: dict[str, int] = {}
        self._opened: dict[str, float] = {}
        self._lock = threading.Lock()

    def allow(self, name: str) -> bool:
        with self._lock:
            opened = self._opened.get(name)
            if opened is None:
                return True
            if time.time() - opened >= self.cooldown_s:  # half-open: let one probe through
                del self._opened[name]
                self._fails[name] = self.threshold - 1
                return True
            return False

    def success(self, name: str) -> None:
        with self._lock:
            self._fails[name] = 0
            self._opened.pop(name, None)

    def failure(self, name: str) -> None:
        with self._lock:
            self._fails[name] = self._fails.get(name, 0) + 1
            if self._fails[name] >= self.threshold:
                self._opened[name] = time.time()

    def reset(self) -> None:
        with self._lock:
            self._fails.clear()
            self._opened.clear()

    def snapshot(self) -> dict:
        with self._lock:
            return {"open": sorted(self._opened), "failures": dict(self._fails)}


BREAKER = CircuitBreaker()


def retry(fn: Callable[[int], T], *, attempts: int = 3, base_delay_s: float = 0.02, label: str = "",
          heal_log: list | None = None, retry_on: tuple = (Exception,)) -> T:
    """Call fn(attempt) until it succeeds. Backoff 20ms, 40ms, ... kept tiny to protect latency."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return fn(attempt)
        except retry_on as e:  # noqa: PERF203
            last = e
            if attempt < attempts - 1:
                log_event(heal_log, "retry", f"{label} attempt {attempt + 1} failed ({e}); backing off")
                time.sleep(base_delay_s * 2 ** attempt)
    assert last is not None
    raise last
