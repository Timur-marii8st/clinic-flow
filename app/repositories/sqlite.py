"""Legacy SQLite demo storage and non-destructive schema upgrade."""
from contextlib import contextmanager, closing
from pathlib import Path
import sqlite3
from app.auth.schema import DDL
from app.integrations.schema import DDL as MIS_DDL
from app.devices.schema import DDL as DEVICE_DDL
from app.routing.schema import DDL as ROUTE_DDL


class SQLiteStore:
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
            if con.execute('PRAGMA user_version').fetchone()[0] > 7:
                raise RuntimeError('Unsupported newer SQLite schema')
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
            con.execute('''CREATE TABLE IF NOT EXISTS command_keys (
                request_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                response TEXT NOT NULL, created_at TEXT NOT NULL)''')
            for statement in (*DDL,*MIS_DDL,*DEVICE_DDL,*ROUTE_DDL):
                con.execute(statement)
            con.execute('PRAGMA user_version=7')

    def close(self):
        pass
