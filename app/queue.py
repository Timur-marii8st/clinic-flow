"""Compatibility entry points for the v0.2 API and tests."""
from pathlib import Path
from app.domain.queue_policy import ACTIVE, DEMO_BOOKINGS, ROOMS, QueueError, now, utc_iso, seconds
from app.repositories.sqlite import SQLiteStore
from app.services.queue import QueueService


class Queue(QueueService):
    def __init__(self, path: str | Path):
        self.path = Path(path)
        super().__init__(SQLiteStore(self.path))


class PostgresQueue(QueueService):
    def __init__(self, url):
        from app.repositories.postgres import PostgresStore
        super().__init__(PostgresStore(url))
