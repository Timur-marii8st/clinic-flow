"""Opaque credentials, current role lookups and atomic security audit."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import hashlib
import json
import secrets
import time
from uuid import uuid4

from app.domain.queue_policy import QueueError, now

ROLES = {'admin', 'registrar', 'doctor', 'analyst'}
SESSION_TTL = 8 * 60 * 60
DEVICE_TTL = 30 * 24 * 60 * 60
PAIR_TTL = 10 * 60
LOGIN_TTL = 5 * 60

_actor = ContextVar('audit_actor', default='system')


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def token():
    return secrets.token_urlsafe(32)


def actor_id():
    return _actor.get()


@contextmanager
def acting_as(actor):
    reset = _actor.set(actor)
    try:
        yield
    finally:
        _actor.reset(reset)


def audit(con, action, object_type, object_id, details=None, actor=None):
    con.execute('''INSERT INTO audit_events(id,actor,action,object_type,object_id,details,created_at)
        VALUES (?,?,?,?,?,?,?)''', (str(uuid4()), actor or actor_id(), action, object_type, str(object_id),
                                  json.dumps(details or {}, ensure_ascii=False, sort_keys=True), now()))


@dataclass(frozen=True)
class Principal:
    id: str
    role: str
    rooms: tuple[str, ...] = ()
    kind: str = 'staff'

    @property
    def actor(self):
        return self.kind + ':' + self.id

    def permits_room(self, room):
        return self.role in ('admin', 'registrar', 'kiosk') or (self.role in ('doctor', 'board') and room in self.rooms) or (self.role == 'board' and not self.rooms)

    def public(self):
        return {'id': self.id, 'role': self.role, 'rooms': list(self.rooms), 'kind': self.kind}


class AuthStore:
    def __init__(self, queue, bootstrap_subject=None):
        self.queue = queue
        self.bootstrap_subject = bootstrap_subject

    def initialize(self):
        if self.bootstrap_subject:
            with self.queue.connection(write=True) as con:
                if not con.execute('SELECT 1 FROM staff_users WHERE subject=?', (self.bootstrap_subject,)).fetchone():
                    con.execute('INSERT INTO staff_users VALUES (?,?,?,?,?)',
                                (self.bootstrap_subject, 'Bootstrap administrator', 'admin', '[]', 1))
                    audit(con, 'bootstrap_admin', 'staff', self.bootstrap_subject)

    def user_upsert(self, principal, subject, label, role, rooms, enabled):
        if role not in ROLES or not subject.strip() or not label.strip() or subject != subject.strip():
            raise QueueError('Некорректная учётная запись', 422)
        if subject == self.bootstrap_subject:
            raise QueueError('Bootstrap-администратор управляется через конфигурацию', 409)
        if role == 'doctor' and not rooms:
            raise QueueError('Врачу нужен хотя бы один кабинет', 422)
        if role != 'doctor' and rooms:
            raise QueueError('Кабинеты назначаются только врачу', 422)
        rooms = sorted(set(rooms))
        with self.queue.connection(write=True) as con:
            for room in rooms:
                self.queue._room(con, room)
            con.execute('''INSERT INTO staff_users(subject,label,role,room_ids,enabled) VALUES (?,?,?,?,?)
                ON CONFLICT(subject) DO UPDATE SET label=excluded.label,role=excluded.role,
                room_ids=excluded.room_ids,enabled=excluded.enabled''',
                (subject, label.strip(), role, json.dumps(rooms), int(enabled)))
            # Role changes and deactivation require reauthentication.
            con.execute('DELETE FROM staff_sessions WHERE subject=?', (subject,))
            audit(con, 'user_upsert', 'staff', subject, {'role':role,'rooms':rooms,'enabled':enabled}, principal.actor)
        return {'subject':subject,'label':label.strip(),'role':role,'rooms':rooms,'enabled':enabled}

    def users(self):
        with self.queue.connection() as con:
            return [{'subject':r['subject'],'label':r['label'],'role':r['role'],
                     'rooms':json.loads(r['room_ids']),'enabled':bool(r['enabled'])}
                    for r in con.execute('SELECT * FROM staff_users ORDER BY subject')]

    def begin_login(self, return_to):
        state, verifier, nonce = token(), token(), token()
        with self.queue.connection(write=True) as con:
            con.execute('DELETE FROM oidc_attempts WHERE expires_at<=?', (int(time.time()),))
            con.execute('INSERT INTO oidc_attempts VALUES (?,?,?,?,?)',
                        (digest(state), verifier, nonce, return_to, int(time.time())+LOGIN_TTL))
        return state, verifier, nonce

    def consume_login(self, state):
        with self.queue.connection(write=True) as con:
            row = con.execute('SELECT * FROM oidc_attempts WHERE state_hash=?', (digest(state),)).fetchone()
            con.execute('DELETE FROM oidc_attempts WHERE state_hash=?', (digest(state),))
            return dict(row) if row and row['expires_at'] > int(time.time()) else None

    def login(self, subject):
        value = token()
        with self.queue.connection(write=True) as con:
            user = con.execute('SELECT * FROM staff_users WHERE subject=? AND enabled=1', (subject,)).fetchone()
            if not user:
                raise QueueError('Учётная запись не разрешена администратором', 403)
            con.execute('DELETE FROM staff_sessions WHERE expires_at<=?', (int(time.time()),))
            con.execute('INSERT INTO staff_sessions VALUES (?,?,?)',
                        (digest(value), subject, int(time.time())+SESSION_TTL))
            audit(con, 'login', 'staff', subject, actor='staff:'+subject)
        return value

    def staff(self, value):
        if not value or len(value)>128:
            return None
        with self.queue.connection() as con:
            row = con.execute('''SELECT u.* FROM staff_sessions s JOIN staff_users u ON s.subject=u.subject
                WHERE s.token_hash=? AND s.expires_at>? AND u.enabled=1''',
                (digest(value), int(time.time()))).fetchone()
            if row:
                return Principal(row['subject'], row['role'], tuple(json.loads(row['room_ids'])))

    def logout(self, value):
        if not value or len(value)>128:
            return
        with self.queue.connection(write=True) as con:
            row = con.execute('SELECT subject FROM staff_sessions WHERE token_hash=?', (digest(value),)).fetchone()
            con.execute('DELETE FROM staff_sessions WHERE token_hash=?', (digest(value),))
            if row:
                audit(con,'logout','staff',row['subject'],actor='staff:'+row['subject'])

    def create_device(self, principal, label, kind, room):
        if not label.strip() or kind not in ('board','kiosk') or (kind=='kiosk' and room):
            raise QueueError('Некорректный тип или привязка устройства',422)
        device_id, code = str(uuid4()), token()
        with self.queue.connection(write=True) as con:
            if room is not None:
                self.queue._room(con,room)
            con.execute('''INSERT INTO devices(id,label,kind,room_id,pairing_hash,pairing_expires)
                VALUES (?,?,?,?,?,?)''',(device_id,label.strip(),kind,room,digest(code),int(time.time())+PAIR_TTL))
            audit(con,'device_create','device',device_id,{'kind':kind,'room':room},principal.actor)
        return {'id':device_id,'pairing_code':code,'expires_in':PAIR_TTL}

    def pair_device(self, code):
        value = token()
        with self.queue.connection(write=True) as con:
            row = con.execute('SELECT * FROM devices WHERE pairing_hash=? AND pairing_expires>? AND revoked=0',
                              (digest(code),int(time.time()))).fetchone()
            if not row:
                raise QueueError('Код не действует',403)
            con.execute('''UPDATE devices SET pairing_hash=NULL,pairing_expires=NULL,token_hash=?,token_expires=?,last_seen=?
                WHERE id=?''',(digest(value),int(time.time())+DEVICE_TTL,int(time.time()),row['id']))
            audit(con,'device_pair','device',row['id'],actor='device:'+row['id'])
            return value, row['kind']

    def device(self, value):
        if not value or len(value)>128:
            return None
        with self.queue.connection() as con:
            row = con.execute('SELECT * FROM devices WHERE token_hash=? AND token_expires>? AND revoked=0',
                              (digest(value),int(time.time()))).fetchone()
            if row:
                return Principal(row['id'],row['kind'],(row['room_id'],) if row['room_id'] else (), 'device')

    def heartbeat(self, principal):
        with self.queue.connection(write=True) as con:
            row=con.execute('SELECT 1 FROM devices WHERE id=? AND revoked=0 AND token_expires>?',(principal.id,int(time.time()))).fetchone()
            if not row:
                raise QueueError('Устройство не авторизовано',401)
            con.execute('UPDATE devices SET last_seen=? WHERE id=?',(int(time.time()),principal.id))
        return {'status':'ok'}

    def revoke_device(self, principal, device_id):
        with self.queue.connection(write=True) as con:
            if not con.execute('SELECT 1 FROM devices WHERE id=?',(device_id,)).fetchone():
                raise QueueError('Устройство не найдено',404)
            con.execute('''UPDATE devices SET revoked=1,pairing_hash=NULL,pairing_expires=NULL,
                token_hash=NULL,token_expires=NULL WHERE id=?''',(device_id,))
            audit(con,'device_revoke','device',device_id,actor=principal.actor)
        return {'id':device_id,'revoked':True}

    def devices(self):
        with self.queue.connection() as con:
            result=[]
            for row in con.execute('SELECT id,label,kind,room_id,last_seen,revoked,token_expires,pairing_expires FROM devices ORDER BY label,id'):
                r=dict(row)
                r['status']='revoked' if r['revoked'] else 'paired' if (r['token_expires'] or 0)>int(time.time()) else 'pending' if (r['pairing_expires'] or 0)>int(time.time()) else 'expired'
                r['online']=r['status']=='paired' and (r['last_seen'] or 0)>int(time.time())-75
                result.append(r)
            return result

    def recent_audit(self, limit=100):
        with self.queue.connection() as con:
            return [{**dict(r),'details':json.loads(r['details'])} for r in con.execute(
                'SELECT * FROM audit_events ORDER BY created_at DESC,id DESC LIMIT ?',(limit,))]
