# ratelimit.py
#
# Client-side protection for the Edge TTS service:
#   - token bucket (requests per second + burst)
#   - bounded queue (max in-flight+waiting requests, then 429)
#   - queue wait timeout (503)
#   - circuit breaker (after N consecutive upstream failures, fail fast for a cooldown)
#
# All primitives are gevent-friendly (the Flask app runs on gevent WSGIServer).

import math
import os
import time

import gevent
from gevent.lock import Semaphore


def _env_float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name, default):
    try:
        return int(float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


class RateLimitExceeded(Exception):
    """Queue full or waited too long for a rate token -> HTTP 429."""

    def __init__(self, retry_after: float, reason: str = "rate_limited"):
        if not math.isfinite(retry_after) or retry_after < 1:
            retry_after = 1.0
        self.retry_after = max(1, int(round(retry_after)))
        self.reason = reason
        super().__init__(f"{reason} (retry after {self.retry_after}s)")


class UpstreamUnavailable(Exception):
    """Circuit breaker is open -> HTTP 503."""

    def __init__(self, retry_after: float, reason: str = "upstream_unavailable"):
        if not math.isfinite(retry_after) or retry_after < 1:
            retry_after = 1.0
        self.retry_after = max(1, int(round(retry_after)))
        self.reason = reason
        super().__init__(f"{reason} (retry after {self.retry_after}s)")


class TokenBucketLimiter:
    """Token bucket + bounded queue.

    TTS_RATE_LIMIT     requests per second (0 disables the rate limit)
    TTS_RATE_BURST     burst capacity (default 2)
    TTS_MAX_QUEUE      max requests waiting for a token (default 10); 0 = no queue allowed
    TTS_QUEUE_TIMEOUT  max seconds to wait for a token (default 30)
    """

    def __init__(self, rate=None, burst=None, max_queue=None, queue_timeout=None):
        self.rate = _env_float("TTS_RATE_LIMIT", 1.0 if rate is None else rate)
        self.burst = _env_float("TTS_RATE_BURST", 2.0 if burst is None else burst)
        self.max_queue = _env_int("TTS_MAX_QUEUE", 10 if max_queue is None else max_queue)
        self.queue_timeout = _env_float("TTS_QUEUE_TIMEOUT", 30.0 if queue_timeout is None else queue_timeout)

        self._lock = Semaphore(1)
        self._tokens = self.burst
        self._last = time.monotonic()
        # max_queue waiting + 1 executing
        self._slots = Semaphore(self.max_queue + 1)

    def _refill_locked(self):
        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        if self.rate > 0:
            self._tokens = min(self.burst, self._tokens + elapsed * self.rate)

    def _wait_time_locked(self) -> float:
        """Seconds until at least one token is available (0 if available now)."""
        if self.rate <= 0 or self._tokens >= 1:
            return 0.0
        return (1 - self._tokens) / self.rate

    def acquire(self):
        """Block until a token is available.

        Raises RateLimitExceeded (queue full / waited too long).
        """
        if not self._slots.acquire(blocking=False):
            # Every slot is taken: reject immediately instead of piling up greenlets.
            retry = 1.0 / self.rate if self.rate > 0 else 1.0
            raise RateLimitExceeded(retry, "queue_full")

        try:
            # rate <= 0 disables the rate limit entirely (queue still bounded)
            if self.rate <= 0:
                return
            deadline = time.monotonic() + self.queue_timeout
            while True:
                with self._lock:
                    self._refill_locked()
                    if self._tokens >= 1:
                        self._tokens -= 1
                        return
                    need = self._wait_time_locked()
                if time.monotonic() + need > deadline:
                    raise RateLimitExceeded(need, "queue_timeout")
                gevent.sleep(max(need, 0.01))
        except BaseException:
            self._slots.release()
            raise

    def release(self):
        self._slots.release()

    def snapshot(self):
        with self._lock:
            self._refill_locked()
            return {
                "rate": self.rate,
                "burst": self.burst,
                "max_queue": self.max_queue,
                "queue_timeout": self.queue_timeout,
                "tokens": round(self._tokens, 2),
                "waiting": max(0, self.max_queue - self._slots.counter + 1) if hasattr(self._slots, "counter") else None,
            }


class CircuitBreaker:
    """Stop hammering Microsoft after consecutive upstream failures.

    TTS_BREAKER_THRESHOLD  consecutive failures before opening (default 5, 0 disables)
    TTS_BREAKER_COOLDOWN   seconds to stay open (default 60)
    """

    def __init__(self, threshold=None, cooldown=None):
        self.threshold = _env_int("TTS_BREAKER_THRESHOLD", 5 if threshold is None else threshold)
        self.cooldown = _env_float("TTS_BREAKER_COOLDOWN", 60.0 if cooldown is None else cooldown)
        self._lock = Semaphore(1)
        self._failures = 0
        self._open_until = 0.0
        self.state = "closed"

    def check(self):
        """Raise UpstreamUnavailable when the breaker is open."""
        if self.threshold <= 0:
            return
        with self._lock:
            if self.state == "open":
                now = time.monotonic()
                if now < self._open_until:
                    raise UpstreamUnavailable(self._open_until - now)
                # cooldown elapsed -> half-open: let one probe through
                self.state = "half_open"

    def record_success(self):
        with self._lock:
            self._failures = 0
            self.state = "closed"

    def record_failure(self):
        with self._lock:
            self._failures += 1
            if self.threshold > 0 and self._failures >= self.threshold:
                self.state = "open"
                self._open_until = time.monotonic() + self.cooldown

    def snapshot(self):
        with self._lock:
            remaining = max(0.0, self._open_until - time.monotonic()) if self.state == "open" else 0.0
            return {
                "state": self.state,
                "consecutive_failures": self._failures,
                "open_for": round(remaining, 1),
                "threshold": self.threshold,
                "cooldown": self.cooldown,
            }
