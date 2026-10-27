import time
from collections.abc import Callable


class CircuitBreaker:
    """Stops sending requests to a provider that keeps failing, then lets one test request through.

    closed    -> normal. After `failure_threshold` failures in a row it opens.
    open      -> requests are skipped for `cooldown_s` seconds.
    half-open -> after the cooldown one request is allowed; success closes it, failure re-opens it.
    """

    def __init__(self, failure_threshold: int = 3, cooldown_s: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return "closed"
        return "half-open" if self._clock() - self._opened_at >= self.cooldown_s else "open"

    def allow(self) -> bool:
        return self.state != "open"

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._opened_at is not None or self._failures >= self.failure_threshold:
            self._opened_at = self._clock()  # a failed half-open test restarts the cooldown
