"""Backpressure on large provider uploads is owned by HTTPX's async transport."""

LARGE_BODY_THRESHOLD = 256 * 1024
UPLOAD_CONCURRENCY = 2
QUEUE_TIMEOUT = 30.0
