"""Shared transactional application service.

The store supplies DB transactions; SQL below uses a small common SQL subset.
Domain sorting and transition policy live in app.domain.queue_policy.
"""
import json
import re
from statistics import median
import hashlib
from app.auth.store import actor_id, audit

from app.domain.queue_policy import (
    ACTIVE, DEMO_BOOKINGS, ROOMS, TRANSITIONS, QueueError,
    eligible, now, seconds, sort_key, utc_iso,
)


class QueueService:
    def __init__(self, store):
        self.store = store

    def connection(self, write=False):
        return self.store.connection(write=write)

    def close(self):
        self.store.close()

    def initialize(self):
        self.store.initialize()
        with self.connection(write=True) as con:
            for room, label in ROOMS:
                con.execute('INSERT INTO rooms(id,label) VALUES (?,?) ON CONFLICT(id) DO NOTHING', (room, label))
            for booking, room in DEMO_BOOKINGS:
                if con.execute('SELECT 1 FROM tickets WHERE booking_code=?', (booking,)).fetchone():
                    continue
                ts = now()
                row = con.execute('''INSERT INTO tickets
                    (room_id,source,booking_code,status,created_at,updated_at,booking_room_id)
                    VALUES (?, 'appointment', ?, 'booked', ?, ?, ?) RETURNING id''',
                    (room, booking, ts, ts, room)).fetchone()
                self._event(con, row['id'], 'seed', None, 'booked', ts)

    def _command(self, name, key, payload, execute):
        """Store successful command result in the very same write transaction."""
        actor = actor_id()
        # Keep legacy demo/system keys; secure users and devices are separate namespaces.
        if key is not None and actor not in ('system','demo'):
            if not 16 <= len(key) <= 100:
                raise QueueError('Некорректный ключ идемпотентности',422)
            key = hashlib.sha256((actor+'\u0000'+key).encode()).hexdigest()
        fingerprint = json.dumps([name, payload], ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        if key is not None and not 16 <= len(key) <= 100:
            raise QueueError('Некорректный ключ идемпотентности', 422)
        with self.connection(write=True) as con:
            if key is not None:
                saved = con.execute('SELECT fingerprint,response FROM command_keys WHERE request_key=?', (key,)).fetchone()
                if saved:
                    if saved['fingerprint'] != fingerprint:
                        raise QueueError('Ключ уже использован для другой команды')
                    return json.loads(saved['response'])
            result = execute(con)
            if key is not None:
                con.execute('INSERT INTO command_keys(request_key,fingerprint,response,created_at) VALUES (?,?,?,?)',
                            (key, fingerprint, json.dumps(result, ensure_ascii=False), now()))
            return result

    def upsert_room(self, room, label):
        """Demo configuration only. No deletion; full facility RBAC is future work."""
        label = label.strip()
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,16}', room) or not 1 <= len(label) <= 100:
            raise QueueError('Некорректный код или название кабинета', 422)
        with self.connection(write=True) as con:
            con.execute('''INSERT INTO rooms(id,label) VALUES (?,?)
                ON CONFLICT(id) DO UPDATE SET label=excluded.label''', (room, label))
            audit(con,'room_upsert','room',room)
            return {'id': room, 'label': label}

    @staticmethod
    def _event(con, ticket_id, action, old_status, new_status, ts):
        con.execute('INSERT INTO events(ticket_id,action,old_status,new_status,created_at) VALUES (?,?,?,?,?)',
                    (ticket_id, action, old_status, new_status, ts))
        row = con.execute('SELECT room_id FROM tickets WHERE id=?',(ticket_id,)).fetchone()
        audit(con, action, 'ticket', ticket_id, {'from':old_status,'to':new_status,'room':row['room_id']})

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

    def revision(self):
        with self.connection() as con:
            return con.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0]

    def rooms(self):
        with self.connection() as con:
            return [dict(row) for row in con.execute('SELECT id,label FROM rooms ORDER BY id')]

    def walk_in(self, room, request_key=None):
        if request_key and actor_id() not in ('system','demo'):
            request_key=hashlib.sha256((actor_id()+'\x00'+request_key).encode()).hexdigest()
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
                VALUES (?, 'walk_in', 'waiting', ?, ?, ?, ?) RETURNING id''', (room, ts, ts, ts, request_key)).fetchone()
            self._new_leg(con, c['id'], room, ts)
            self._event(con, c['id'], 'issue', None, 'waiting', ts)
            return self._public(self._ticket(con, c['id']))

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
                VALUES (?, 'appointment', ?, 'booked', ?, ?, ?, ?, ?) RETURNING id''', (room, code, ts, ts, slot, room, slot)).fetchone()
            self._event(con, c['id'], 'book_demo', None, 'booked', ts)
            return self._public(self._ticket(con, c['id']))

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
                     'scheduled_at': r['scheduled_at'], 'version': r['updated_at'], 'eligible': eligible(r, ts)}
                    for r in sorted(rows, key=lambda r: sort_key(r, ts))]

    def call_next(self, room, request_key=None):
        def execute(con):
            self._room(con, room)
            if con.execute("SELECT 1 FROM tickets WHERE room_id=? AND status IN ('called','serving')", (room,)).fetchone():
                raise QueueError('Сначала завершите текущий вызов')
            ts = now()
            rows = con.execute("SELECT * FROM tickets WHERE room_id=? AND status='waiting'", (room,)).fetchall()
            row = min((r for r in rows if eligible(r, ts)), key=lambda r: sort_key(r, ts), default=None)
            if not row:
                raise QueueError('Нет пациентов, доступных для вызова')
            return self._transition(con, row, 'call', 'called')
        return self._command('call_next', request_key, [room], execute)

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

    def act(self, room, ticket_id, action, expected_version=None, request_key=None):
        def execute(con):
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
            target = TRANSITIONS.get((row['status'], action))
            if not target:
                raise QueueError('Недопустимое действие для текущего статуса')
            return self._transition(con, row, action, target)
        return self._command('action', request_key, [room, ticket_id, action, expected_version], execute)

    def transfer(self, room, ticket_id, target_room, expected_version=None, request_key=None):
        def execute(con):
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

        return self._command('transfer', request_key, [room, ticket_id, target_room, expected_version], execute)

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
