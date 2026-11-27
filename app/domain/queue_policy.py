"""Pure queue policy, independent of HTTP and database.

Synthetic demo rules; never substitute for medical urgency assessment.
"""
from datetime import datetime, timezone

ROOMS = [('101', 'Кабинет 101'), ('102', 'Кабинет 102'), ('103', 'Кабинет 103')]
DEMO_BOOKINGS = [('DEMO-101', '101'), ('DEMO-102', '102'), ('DEMO-103', '103')]
ACTIVE = ('waiting', 'called', 'serving')


class QueueError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise QueueError('Укажите часовой пояс времени записи', 422)
    return value.astimezone(timezone.utc).isoformat(timespec='microseconds')


def seconds(start, end):
    if not start or not end:
        return None
    return max(0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())

def sort_key(row, ts):
    # Due appointments before FIFO walk-ins. After 30 min of waiting, any
    # eligible ticket joins the oldest-waiting class. Never calls future slots.
    if row['status'] in ('called', 'serving'):
        return (0, row['updated_at'], row['id'])
    if row['scheduled_at'] and row['scheduled_at'] > ts:
        return (4, row['scheduled_at'], row['id'])
    if (seconds(row['checked_in_at'], ts) or 0) >= 1800:
        return (1, row['checked_in_at'], row['id'])
    if row['scheduled_at']:
        return (2, row['scheduled_at'], row['id'])
    return (3, row['checked_in_at'], row['id'])

def eligible(row, ts):
    return row['status'] == 'waiting' and (not row['scheduled_at'] or row['scheduled_at'] <= ts)


TRANSITIONS = {('called','start'): 'serving', ('serving','finish'): 'done',
               ('called','no_show'): 'no_show', ('waiting','cancel'): 'cancelled',
               ('booked','cancel'): 'cancelled'}
