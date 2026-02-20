"""
In-process sliding-window rate limiter.

Limits are applied per LINE user ID (extracted from the JSON body of webhook
requests).  For auth endpoints the client IP is used instead.

Default limits (configurable via Settings):
  - Webhook messages : 20 requests / 60 minutes per user
  - OTP requests     : 3 requests  / 10 minutes per IP  (brute-force guard)

This implementation uses a single in-memory deque per key and is suitable for
a single-process deployment (Cloud Run / Docker with --workers 1).
For multi-process deployments, replace the store with Redis.
"""

import json
import time
from collections import defaultdict, deque
from threading import Lock
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.config import get_settings
from app.utils.logger import get_logger

settings = get_settings()
logger = get_logger(__name__)


class SlidingWindowLimiter:
    """
    Thread-safe sliding window counter.

    Args:
        max_requests: maximum calls allowed within *window_seconds*
        window_seconds: duration of the window
    """

    def __init__(self, max_requests: int, window_seconds: int) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._store: dict[str, deque] = defaultdict(deque)
        self._lock = Lock()

    def is_allowed(self, key: str) -> tuple[bool, int]:
        """
        Check whether *key* is within the rate limit.

        Returns:
            (allowed, retry_after_seconds)
        """
        now = time.monotonic()
        cutoff = now - self.window_seconds

        with self._lock:
            window = self._store[key]

            # Evict timestamps older than the window
            while window and window[0] < cutoff:
                window.popleft()

            if len(window) >= self.max_requests:
                retry_after = int(self.window_seconds - (now - window[0])) + 1
                return False, retry_after

            window.append(now)
            return True, 0

    def reset(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)


# ── Singleton limiters ─────────────────────────────────────────────────────────

# Webhook: 20 messages per hour per LINE user
_webhook_limiter = SlidingWindowLimiter(
    max_requests=getattr(settings, "rate_limit_chat_max", 20),
    window_seconds=getattr(settings, "rate_limit_chat_window", 3600),
)

# OTP request: 3 per 10 minutes per IP
_otp_limiter = SlidingWindowLimiter(
    max_requests=getattr(settings, "rate_limit_otp_max", 3),
    window_seconds=getattr(settings, "rate_limit_otp_window", 600),
)


def get_webhook_limiter() -> SlidingWindowLimiter:
    return _webhook_limiter


def get_otp_limiter() -> SlidingWindowLimiter:
    return _otp_limiter


# ── FastAPI / Starlette middleware ─────────────────────────────────────────────


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Applies rate limits at the ASGI middleware layer.

    - POST /webhook  → per LINE user ID (extracted from body)
    - POST /auth/request-otp → per client IP
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path

        # ── Webhook rate limit ──────────────────────────────────────────────
        if path == "/webhook" and request.method == "POST":
            body = await request.body()

            # Re-attach body so downstream handlers can read it again
            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            request._receive = receive

            line_user_id = _extract_line_user_id(body)
            if line_user_id:
                allowed, retry_after = _webhook_limiter.is_allowed(line_user_id)
                if not allowed:
                    logger.warning(
                        "Rate limit hit (webhook)",
                        line_user_id=line_user_id,
                        retry_after=retry_after,
                    )
                    return Response(
                        content=json.dumps(
                            {
                                "error": "rate_limited",
                                "retry_after": retry_after,
                            }
                        ),
                        status_code=429,
                        media_type="application/json",
                        headers={"Retry-After": str(retry_after)},
                    )

        # ── OTP rate limit ──────────────────────────────────────────────────
        elif path == "/auth/request-otp" and request.method == "POST":
            client_ip = _get_client_ip(request)
            allowed, retry_after = _otp_limiter.is_allowed(client_ip)
            if not allowed:
                logger.warning(
                    "Rate limit hit (OTP request)", ip=client_ip, retry_after=retry_after
                )
                return Response(
                    content=json.dumps(
                        {
                            "error": "too_many_otp_requests",
                            "message": "คุณขอรหัส OTP บ่อยเกินไป กรุณารอสักครู่",
                            "retry_after": retry_after,
                        }
                    ),
                    status_code=429,
                    media_type="application/json",
                    headers={"Retry-After": str(retry_after)},
                )

        return await call_next(request)


# ── Helpers ────────────────────────────────────────────────────────────────────


def _extract_line_user_id(body: bytes) -> str | None:
    """Best-effort extraction of the first userId from a LINE webhook payload."""
    try:
        payload = json.loads(body)
        events = payload.get("events", [])
        if events:
            return events[0].get("source", {}).get("userId")
    except Exception:
        pass
    return None


def _get_client_ip(request: Request) -> str:
    """Extract real client IP respecting X-Forwarded-For (Cloud Run / proxy)."""
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    if request.client:
        return request.client.host
    return "unknown"
