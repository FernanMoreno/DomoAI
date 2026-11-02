"""Small process-local request limiter for the local management surface."""

from __future__ import annotations

import time
from collections import deque


class RateLimiter:
    def __init__(
        self,
        max_requests: int = 60,
        *,
        window_seconds: float = 60.0,
        max_keys: int = 1024,
    ) -> None:
        if max_requests <= 0 or window_seconds <= 0 or max_keys <= 0:
            raise ValueError("rate limit values must be positive")
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._requests: dict[str, deque[float]] = {}

    def allow(self, key: str, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        window = self._requests.get(key)
        if window is None:
            if len(self._requests) >= self.max_keys:
                oldest_key = min(
                    self._requests,
                    key=lambda item: (
                        self._requests[item][-1] if self._requests[item] else float("-inf")
                    ),
                )
                self._requests.pop(oldest_key, None)
            window = deque()
            self._requests[key] = window
        while window and current - window[0] >= self.window_seconds:
            window.popleft()
        if len(window) >= self.max_requests:
            return False
        window.append(current)
        return True


__all__ = ["RateLimiter"]
