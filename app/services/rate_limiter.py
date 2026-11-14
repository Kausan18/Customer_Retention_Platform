from collections import defaultdict, deque
import time

RATE_LIMIT_REQUESTS = 8
RATE_LIMIT_WINDOW_SECONDS = 60


class RateLimiter:
    def __init__(self) -> None:
        self.requests: dict[str, deque[float]] = defaultdict(deque)

    def check(self, customer_id: str) -> bool:
        now = time.monotonic()
        requests = self.requests[customer_id]
        while requests and now - requests[0] > RATE_LIMIT_WINDOW_SECONDS:
            requests.popleft()
        if len(requests) >= RATE_LIMIT_REQUESTS:
            return False
        requests.append(now)
        return True


rate_limiter = RateLimiter()
