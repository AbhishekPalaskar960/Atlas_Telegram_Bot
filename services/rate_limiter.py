"""Async/sync-safe request queue & throttle for Groq (or any) LLM API calls.

Dropped in front of llm_service's network calls to stay under the RPM limit
proactively instead of firing and backing off after a 429.

The bot is synchronous (python-telegram-bot handlers + scheduler run in
separate threads), so this is a thread-safe sliding-window limiter: threads
that would exceed the budget block (wait) for a slot. The async context
manager is provided for consumers that do want an await-style throttle.
"""

import logging
import threading
import time
from collections import deque
from typing import Optional

logger = logging.getLogger(__name__)


class GroqRateLimiter:
    """Sliding-window rate limiter.

    Guarantees at most `max_requests_per_minute` calls may proceed in any
    rolling `window_seconds` window. Callers that would exceed the limit wait
    until a slot frees up, rather than firing and getting a 429 back.
    Threads sleep WITHOUT holding the lock so others can still prune/queue.

    An optional concurrency cap (`max_concurrent`) prevents bursts even when
    the per-minute budget hasn't been reached.
    """

    def __init__(
        self,
        max_requests_per_minute: int = 30,
        max_concurrent: Optional[int] = None,
        window_seconds: float = 60.0,
    ):
        self.max_requests = max_requests_per_minute
        self.window_seconds = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()
        self._semaphore = (
            threading.BoundedSemaphore(max_concurrent) if max_concurrent else None
        )

    def _prune_locked(self, now: float) -> None:
        while self._timestamps and now - self._timestamps[0] >= self.window_seconds:
            self._timestamps.popleft()

    def wait_for_slot(self) -> None:
        """Block until a request slot is available (throttles when at/over cap)."""
        while True:
            with self._lock:
                now = time.monotonic()
                self._prune_locked(now)
                if len(self._timestamps) < self.max_requests:
                    self._timestamps.append(now)
                    logger.debug(
                        "Groq limiter: acquired slot (%d/%d in window)",
                        len(self._timestamps),
                        self.max_requests,
                    )
                    return
                sleep_for = self.window_seconds - (now - self._timestamps[0]) + 0.05

            logger.info(
                "Groq limiter: throttling (%d/%d in window), waiting %.2fs",
                len(self._timestamps),
                self.max_requests,
                sleep_for,
            )
            time.sleep(sleep_for)

    def acquire(self) -> None:
        self.wait_for_slot()
        if self._semaphore:
            self._semaphore.acquire()

    def release(self) -> None:
        if self._semaphore:
            self._semaphore.release()

    # ---- sync context manager (the bot's actual call path) ----
    def __enter__(self) -> "GroqRateLimiter":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False

    # ---- async context manager (for future async callers) ----
    async def __aenter__(self) -> "GroqRateLimiter":
        await self._async_acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False

    async def _run_acquire(self) -> None:
        self.acquire()

    async def _async_acquire(self) -> None:
        # Run the blocking/throttling acquire off the event loop.
        import asyncio

        await asyncio.to_thread(self.wait_for_slot)
        if self._semaphore:
            await asyncio.to_thread(self._semaphore.acquire)


# Module-wide limiter, tuned conservatively for Groq free tier (~30 RPM).
# Keep the existing 429/retry backoff in llm_service as the safety net.
groq_limiter = GroqRateLimiter(max_requests_per_minute=15, max_concurrent=3)