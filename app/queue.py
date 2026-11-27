"""Local synthetic-data queue. One SQLite transaction per domain mutation.

A ticket is a visit's anonymous code; a leg records each stay in one room.
No automatic clinical prioritization or cancellation.
"""
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
import sqlite3

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


class Queue:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def connection(self, write=False):
        con = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA foreign_keys=ON')
        try:
            con.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # WAL is persistent; schema creation + migration is serialized below.
        with closing(sqlite3.connect(self.path, timeout=10, isolation_level=None)) as con:
            con.execute('PRAGMA journal_mode=WAL')
        with self.connection(write=True) as con:
            con.execute('CREATE TABLE IF NOT EXISTS rooms (id TEXT PRIMARY KEY, label TEXT NOT NULL)')
            con.execute('''CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room_id TEXT NOT NULL REFERENCES rooms(id),
                source TEXT NOT NULL CHECK(source IN ('appointment','walk_in')),
                booking_code TEXT UNIQUE,
                status TEXT NOT NULL CHECK(status IN ('booked','waiting','called','serving','done','no_show','cancelled')),
                created_at TEXT NOT NULL, checked_in_at TEXT, updated_at TEXT NOT NULL)''')
            con.execute('''CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL REFERENCES tickets(id),
                action TEXT NOT NULL, old_status TEXT, new_status TEXT NOT NULL, created_at TEXT NOT NULL)''')
            columns = {r['name'] for r in con.execute('PRAGMA table_info(tickets)')}
            for name in ('scheduled_at', 'request_key', 'booking_room_id', 'booking_scheduled_at'):
                if name not in columns:
                    con.execute(f'ALTER TABLE tickets ADD COLUMN {name} TEXT')
            con.execute('''UPDATE tickets SET booking_room_id=room_id,booking_scheduled_at=scheduled_at
                WHERE booking_code IS NOT NULL AND booking_room_id IS NULL''')
            con.execute('''CREATE UNIQUE INDEX IF NOT EXISTS one_active_per_room
                ON tickets(room_id) WHERE status IN ('called','serving')''')
            con.execute('''CREATE UNIQUE INDEX IF NOT EXISTS unique_walkin_request
                ON tickets(request_key) WHERE request_key IS NOT NULL''')
            con.execute('CREATE INDEX IF NOT EXISTS queue_order ON tickets(room_id,status,checked_in_at,id)')
            con.execute('CREATE INDEX IF NOT EXISTS event_ticket ON events(ticket_id,id)')
            con.execute('''CREATE TABLE IF NOT EXISTS queue_legs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL REFERENCES tickets(id),
                room_id TEXT NOT NULL REFERENCES rooms(id), entered_at TEXT NOT NULL,
                called_at TEXT, started_at TEXT, ended_at TEXT, outcome TEXT)''')
            con.execute('''CREATE UNIQUE INDEX IF NOT EXISTS one_open_leg
                ON queue_legs(ticket_id) WHERE ended_at IS NULL''')
            con.execute('CREATE INDEX IF NOT EXISTS legs_room_end ON queue_legs(room_id,ended_at)')
            # Backfill original v0.1 tickets; old schema had no transfers/requeues.
            con.execute('''INSERT INTO queue_legs(ticket_id,room_id,entered_at,called_at,started_at,ended_at,outcome)
                SELECT t.id,t.room_id,t.checked_in_at,
                    (SELECT MIN(e.created_at) FROM events e WHERE e.ticket_id=t.id AND e.action='call'),
                    (SELECT MIN(e.created_at) FROM events e WHERE e.ticket_id=t.id AND e.action='start'),
                    CASE WHEN t.status IN ('done','no_show','cancelled') THEN t.updated_at END,
                    CASE WHEN t.status IN ('done','no_show','cancelled') THEN t.status END
                FROM tickets t WHERE t.checked_in_at IS NOT NULL
                    AND NOT EXISTS(SELECT 1 FROM queue_legs l WHERE l.ticket_id=t.id)''')
            con.executemany('INSERT OR IGNORE INTO rooms VALUES (?,?)', ROOMS)
            for booking, room in DEMO_BOOKINGS:
                ts = now()
                c = con.execute('''INSERT OR IGNORE INTO tickets
                    (room_id,source,booking_code,status,created_at,updated_at,booking_room_id)
                    VALUES (?, 'appointment', ?, 'booked', ?, ?, ?)''', (room, booking, ts, ts, room))
                if c.rowcount:
                    self._event(con, c.lastrowid, 'seed', None, 'booked', ts)
            con.execute('PRAGMA user_version=2')

    @staticmethod
    def _event(con, ticket_id, action, old_status, new_status, ts):
        con.execute('INSERT INTO events(ticket_id,action,old_status,new_status,created_at) VALUES (?,?,?,?,?)',
                    (ticket_id, action, old_status, new_status, ts))

    @staticmethod
    def _public(row):
        return {'id': row['id'], 'code': f"A-{row['id']:04d}", 'room_id': row['room_id'], 'status': row['status']}

    @staticmethod
    def _room(con, room):
        if not con.execute('SELECT 1 FROM rooms WHERE id=?', (room,)).fetchone():
            raise QueueError('Кабинет не найден', 404)

    @staticmethod
    def _ticket(con, ticket_id):
        return con.execute('SELECT * FROM tickets WHERE id=?', (ticket_id,)).fetchone()

    @staticmethod
    def _new_leg(con, ticket_id, room, ts):
        con.execute('INSERT INTO queue_legs(ticket_id,room_id,entered_at) VALUES (?,?,?)', (ticket_id, room, ts))

    @staticmethod
    def _end_leg(con, ticket_id, ts, outcome):
        con.execute('UPDATE queue_legs SET ended_at=?,outcome=? WHERE ticket_id=? AND ended_at IS NULL',
                    (ts, outcome, ticket_id))

    @staticmethod
    def _sort_key(row, ts):
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

    @staticmethod
    def _eligible(row, ts):
        return row['status'] == 'waiting' and (not row['scheduled_at'] or row['scheduled_at'] <= ts)

    def revision(self):
        with self.connection() as con:
            return con.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0]

    def rooms(self):
        with self.connection() as con:
            return [dict(row) for row in con.execute('SELECT id,label FROM rooms ORDER BY id')]

    def walk_in(self, room, request_key=None):
        with self.connection(write=True) as con:
            self._room(con, room)
            if request_key:
                old = con.execute('SELECT * FROM tickets WHERE request_key=?', (request_key,)).fetchone()
                if old:
                    # The room may have changed after transfer; compare original leg.
                    original = con.execute('SELECT room_id FROM queue_legs WHERE ticket_id=? ORDER BY id LIMIT 1', (old['id'],)).fetchone()
                    if not original or original['room_id'] != room:
                        raise QueueError('Ключ запроса уже использован для другого кабинета')
                    return self._public(old)
            ts = now()
            c = con.execute('''INSERT INTO tickets(room_id,source,status,created_at,checked_in_at,updated_at,request_key)
                VALUES (?, 'walk_in', 'waiting', ?, ?, ?, ?)''', (room, ts, ts, ts, request_key))
            self._new_leg(con, c.lastrowid, room, ts)
            self._event(con, c.lastrowid, 'issue', None, 'waiting', ts)
            return self._public(self._ticket(con, c.lastrowid))

    def book_demo(self, code, room, scheduled_at):
        """Synthetic appointment import seam; not a Vitacore API."""
        code = code.strip().upper()
        if not code.startswith('DEMO-') or len(code) < 6:
            raise QueueError('Только синтетические коды DEMO-…', 422)
        slot = utc_iso(scheduled_at)
        with self.connection(write=True) as con:
            self._room(con, room)
            row = con.execute('SELECT * FROM tickets WHERE booking_code=?', (code,)).fetchone()
            if row:
                if row['booking_room_id'] != room or row['booking_scheduled_at'] != slot:
                    raise QueueError('Код уже существует с другим временем или кабинетом')
                return self._public(row)
            ts = now()
            c = con.execute('''INSERT INTO tickets(room_id,source,booking_code,status,created_at,updated_at,scheduled_at,booking_room_id,booking_scheduled_at)
                VALUES (?, 'appointment', ?, 'booked', ?, ?, ?, ?, ?)''', (room, code, ts, ts, slot, room, slot))
            self._event(con, c.lastrowid, 'book_demo', None, 'booked', ts)
            return self._public(self._ticket(con, c.lastrowid))

    def check_in(self, code):
        with self.connection(write=True) as con:
            row = con.execute('SELECT * FROM tickets WHERE booking_code=?', (code,)).fetchone()
            if not row:
                raise QueueError('Тестовая запись не найдена', 404)
            if row['status'] in ACTIVE:
                return self._public(row)
            if row['status'] != 'booked':
                raise QueueError('Запись уже обработана')
            ts = now()
            con.execute('UPDATE tickets SET status=?,checked_in_at=?,updated_at=? WHERE id=?', ('waiting', ts, ts, row['id']))
            self._new_leg(con, row['id'], row['room_id'], ts)
            self._event(con, row['id'], 'check_in', 'booked', 'waiting', ts)
            return self._public(self._ticket(con, row['id']))

    def staff_view(self, room):
        with self.connection() as con:
            self._room(con, room)
            ts = now()
            rows = con.execute("SELECT * FROM tickets WHERE room_id=? AND status IN ('waiting','called','serving')", (room,)).fetchall()
            return [{**self._public(r), 'source': r['source'], 'checked_in_at': r['checked_in_at'],
                     'scheduled_at': r['scheduled_at'], 'version': r['updated_at'], 'eligible': self._eligible(r, ts)}
                    for r in sorted(rows, key=lambda r: self._sort_key(r, ts))]

    def call_next(self, room):
        with self.connection(write=True) as con:
            self._room(con, room)
            if con.execute("SELECT 1 FROM tickets WHERE room_id=? AND status IN ('called','serving')", (room,)).fetchone():
                raise QueueError('Сначала завершите текущий вызов')
            ts = now()
            rows = con.execute("SELECT * FROM tickets WHERE room_id=? AND status='waiting'", (room,)).fetchall()
            row = min((r for r in rows if self._eligible(r, ts)), key=lambda r: self._sort_key(r, ts), default=None)
            if not row:
                raise QueueError('Нет пациентов, доступных для вызова')
            return self._transition(con, row, 'call', 'called')

    def _transition(self, con, row, action, target):
        ts = now()
        con.execute('UPDATE tickets SET status=?,updated_at=? WHERE id=?', (target, ts, row['id']))
        if action == 'call':
            con.execute('UPDATE queue_legs SET called_at=? WHERE ticket_id=? AND ended_at IS NULL', (ts, row['id']))
        elif action == 'start':
            con.execute('UPDATE queue_legs SET started_at=? WHERE ticket_id=? AND ended_at IS NULL', (ts, row['id']))
        elif target in ('done', 'no_show', 'cancelled'):
            self._end_leg(con, row['id'], ts, target)
        self._event(con, row['id'], action, row['status'], target, ts)
        return self._public(self._ticket(con, row['id']))

    @staticmethod
    def _verify_version(row, expected_version):
        if expected_version is not None and row['updated_at'] != expected_version:
            raise QueueError('Талон изменён другим сотрудником. Обновите очередь.')

    def act(self, room, ticket_id, action, expected_version=None):
        rules = {('called','start'): 'serving', ('serving','finish'): 'done',
                 ('called','no_show'): 'no_show', ('waiting','cancel'): 'cancelled',
                 ('booked','cancel'): 'cancelled'}
        with self.connection(write=True) as con:
            self._room(con, room)
            row = con.execute('SELECT * FROM tickets WHERE id=? AND room_id=?', (ticket_id, room)).fetchone()
            if not row:
                raise QueueError('Талон не найден', 404)
            self._verify_version(row, expected_version)
            if action == 'repeat' and row['status'] == 'called':
                return self._transition(con, row, action, 'called')
            if action == 'requeue' and row['status'] in ('called', 'no_show'):
                ts = now()
                self._end_leg(con, ticket_id, ts, 'requeued')
                self._new_leg(con, ticket_id, room, ts)
                con.execute("UPDATE tickets SET status='waiting',checked_in_at=?,updated_at=?,scheduled_at=NULL WHERE id=?", (ts, ts, ticket_id))
                self._event(con, ticket_id, 'requeue', row['status'], 'waiting', ts)
                return self._public(self._ticket(con, ticket_id))
            target = rules.get((row['status'], action))
            if not target:
                raise QueueError('Недопустимое действие для текущего статуса')
            return self._transition(con, row, action, target)

    def transfer(self, room, ticket_id, target_room, expected_version=None):
        with self.connection(write=True) as con:
            self._room(con, room)
            self._room(con, target_room)
            if target_room == room:
                raise QueueError('Выберите другой кабинет', 422)
            row = con.execute('SELECT * FROM tickets WHERE id=? AND room_id=?', (ticket_id, room)).fetchone()
            if not row:
                raise QueueError('Талон не найден', 404)
            self._verify_version(row, expected_version)
            if row['status'] not in ACTIVE:
                raise QueueError('Перенести можно только активный талон')
            ts = now()
            self._end_leg(con, ticket_id, ts, 'transferred')
            self._new_leg(con, ticket_id, target_room, ts)
            con.execute("UPDATE tickets SET room_id=?,status='waiting',checked_in_at=?,updated_at=?,scheduled_at=NULL WHERE id=?", (target_room, ts, ts, ticket_id))
            self._event(con, ticket_id, 'transfer', row['status'], 'waiting', ts)
            return self._public(self._ticket(con, ticket_id))

    def history(self, ticket_id):
        with self.connection() as con:
            row = self._ticket(con, ticket_id)
            if not row:
                raise QueueError('Талон не найден', 404)
            legs = [dict(r) for r in con.execute('''SELECT room_id,entered_at,called_at,started_at,ended_at,outcome
                FROM queue_legs WHERE ticket_id=? ORDER BY id''', (ticket_id,))]
            return {'ticket': self._public(row), 'legs': legs}

    def recent_no_shows(self, room):
        with self.connection() as con:
            self._room(con, room)
            return [{**self._public(r), 'version': r['updated_at']} for r in con.execute(
                "SELECT * FROM tickets WHERE room_id=? AND status='no_show' ORDER BY updated_at DESC LIMIT 10", (room,))]

    def analytics(self):
        """Per-room metrics from the last 100 ended legs; not an SLA or ML prediction."""
        with self.connection() as con:
            result = []
            for room in con.execute('SELECT id,label FROM rooms ORDER BY id').fetchall():
                legs = con.execute('''SELECT * FROM queue_legs WHERE room_id=? AND ended_at IS NOT NULL
                    ORDER BY ended_at DESC,id DESC LIMIT 100''', (room['id'],)).fetchall()
                waits = [seconds(l['entered_at'], l['started_at']) for l in legs if l['started_at']]
                service = [seconds(l['started_at'], l['ended_at']) for l in legs if l['started_at'] and l['outcome'] in ('done', 'transferred')]
                median_service = round(median(service), 1) if len(service) >= 3 else None
                waiting = con.execute("SELECT COUNT(*) FROM tickets WHERE room_id=? AND status='waiting'", (room['id'],)).fetchone()[0]
                result.append({'room_id': room['id'], 'label': room['label'], 'waiting': waiting,
                               'ended_legs': len(legs), 'completed': sum(l['outcome']=='done' for l in legs),
                               'no_shows': sum(l['outcome']=='no_show' for l in legs),
                               'transfers': sum(l['outcome']=='transferred' for l in legs),
                               'wait_samples': len(waits), 'service_samples': len(service),
                               'median_wait_seconds': round(median(waits), 1) if waits else None,
                               'median_service_seconds': median_service})
            return result

    def board(self):
        with self.connection() as con:
            return [{'room_id': r['id'], 'label': r['label'],
                     'code': f"A-{r['ticket_id']:04d}" if r['ticket_status']=='called' else None,
                     'state': r['ticket_status'] or 'idle',
                     'call_version': r['updated_at'] if r['ticket_status']=='called' else None}
                    for r in con.execute('''SELECT rooms.id, rooms.label, tickets.id AS ticket_id,
                        tickets.status AS ticket_status, tickets.updated_at FROM rooms
                        LEFT JOIN tickets ON rooms.id=tickets.room_id AND tickets.status IN ('called','serving') ORDER BY rooms.id''')]
