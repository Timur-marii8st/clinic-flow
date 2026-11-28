"""Inbound reconciliation, versioned external snapshots and manual conflicts."""
import hashlib
import json
import re

from app.auth.store import acting_as, audit
from app.domain.queue_policy import QueueError, now
from .contracts import AppointmentEvent, AppointmentSnapshot, canonical_json


def validate_source(source):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,32}',source):
        raise QueueError('Некорректный источник МИС',422)


def fingerprint(event):
    return hashlib.sha256(canonical_json(event.canonical()).encode()).hexdigest()


class Integration:
    def __init__(self,queue):
        self.queue=queue

    def cursor(self,source):
        validate_source(source)
        with self.queue.connection() as con:
            row=con.execute('SELECT position FROM mis_cursors WHERE source=?',(source,)).fetchone()
            return row['position'] if row else 0

    @staticmethod
    def _advance(con,source,position):
        if position is None:
            return
        row=con.execute('SELECT position FROM mis_cursors WHERE source=?',(source,)).fetchone()
        current=row['position'] if row else 0
        if position<=current:
            return
        if position!=current+1:
            raise QueueError('Разрыв последовательности событий МИС',409)
        con.execute('''INSERT INTO mis_cursors(source,position) VALUES (?,?)
            ON CONFLICT(source) DO UPDATE SET position=excluded.position''',(source,position))

    def apply(self,source,event: AppointmentEvent,position=None):
        validate_source(source)
        item=event.appointment
        payload=canonical_json(item.canonical())
        fp=fingerprint(event)
        with acting_as('integration:'+source), self.queue.connection(write=True) as con:
            old_event=con.execute('SELECT fingerprint,outcome FROM mis_inbox WHERE source=? AND event_id=?',
                                  (source,event.event_id)).fetchone()
            if old_event:
                if old_event['fingerprint']!=fp:
                    raise QueueError('Повтор event_id с другим содержимым',409)
                self._advance(con,source,position)
                return {'outcome':'duplicate','previous':old_event['outcome']}
            row=con.execute('SELECT * FROM mis_appointments WHERE source=? AND external_id=?',
                            (source,item.external_id)).fetchone()
            if row and item.version<row['version']:
                outcome='stale'
            elif row and item.version==row['version']:
                if payload==row['snapshot']:
                    outcome='same_version'
                else:
                    # Do not replace the previously accepted snapshot on an ambiguous version.
                    con.execute('UPDATE mis_appointments SET conflict=? WHERE source=? AND external_id=?',
                                ('version_collision',source,item.external_id))
                    outcome='version_collision'
                    audit(con,'mis_conflict','appointment',item.external_id,{'source':source,'version':item.version,'reason':outcome})
            else:
                ticket_id=row['ticket_id'] if row else None
                ticket_id,conflict=self._apply_snapshot(con,item,ticket_id)
                con.execute('''INSERT INTO mis_appointments(source,external_id,version,snapshot,ticket_id,conflict)
                    VALUES (?,?,?,?,?,?) ON CONFLICT(source,external_id) DO UPDATE SET
                    version=excluded.version,snapshot=excluded.snapshot,ticket_id=excluded.ticket_id,conflict=excluded.conflict''',
                    (source,item.external_id,item.version,payload,ticket_id,conflict))
                outcome='conflict' if conflict else 'applied'
                audit(con,'mis_conflict' if conflict else 'mis_applied','appointment',item.external_id,
                      {'source':source,'version':item.version,'reason':conflict})
            con.execute('INSERT INTO mis_inbox VALUES (?,?,?,?,?)',(source,event.event_id,fp,outcome,now()))
            self._advance(con,source,position)
            return {'outcome':outcome}

    def _apply_snapshot(self,con,item,ticket_id):
        """Apply only to an untouched booking; never silently change arrived visits."""
        slot=item.canonical()['scheduled_at']
        ts=now()
        if ticket_id is None:
            if item.state=='cancelled':
                return None,None
            if not con.execute('SELECT 1 FROM rooms WHERE id=?',(item.room_id,)).fetchone():
                return None,'unknown_room'
            if con.execute('SELECT 1 FROM tickets WHERE booking_code=?',(item.booking_code,)).fetchone():
                return None,'booking_code_collision'
            row=con.execute("""INSERT INTO tickets(room_id,source,booking_code,status,created_at,updated_at,scheduled_at,booking_room_id,booking_scheduled_at)
                VALUES (?,'appointment',?,'booked',?,?,?,?,?) RETURNING id""",
                (item.room_id,item.booking_code,ts,ts,slot,item.room_id,slot)).fetchone()
            ticket_id=row['id']
            self.queue._event(con,ticket_id,'mis_book',None,'booked',ts)
            return ticket_id,None

        row=self.queue._ticket(con,ticket_id)
        if not row:
            raise RuntimeError('External appointment mapping is inconsistent')
        if row['booking_code']!=item.booking_code:
            return ticket_id,'booking_code_changed'
        if row['status']=='cancelled':
            return ticket_id,None if item.state=='cancelled' else 'already_cancelled'
        if row['status']!='booked' or con.execute('SELECT 1 FROM queue_legs WHERE ticket_id=? LIMIT 1',(ticket_id,)).fetchone():
            unchanged=(item.state=='booked' and row['booking_room_id']==item.room_id and row['booking_scheduled_at']==slot)
            return ticket_id,None if unchanged else 'visit_started'
        if item.state=='cancelled':
            self.queue._transition(con,row,'mis_cancel','cancelled')
            return ticket_id,None
        if not con.execute('SELECT 1 FROM rooms WHERE id=?',(item.room_id,)).fetchone():
            return ticket_id,'unknown_room'
        if row['room_id']==item.room_id and row['scheduled_at']==slot:
            return ticket_id,None
        con.execute('''UPDATE tickets SET room_id=?,scheduled_at=?,booking_room_id=?,booking_scheduled_at=?,updated_at=?
            WHERE id=?''',(item.room_id,slot,item.room_id,slot,ts,ticket_id))
        self.queue._event(con,ticket_id,'mis_reschedule','booked','booked',ts)
        return ticket_id,None

    def conflicts(self):
        with self.queue.connection() as con:
            return [{'source':r['source'],'external_id':r['external_id'],'version':r['version'],
                     'conflict':r['conflict'],'ticket_id':r['ticket_id'],
                     'desired':{k:v for k,v in json.loads(r['snapshot']).items() if k!='booking_code'}}
                    for r in con.execute('SELECT * FROM mis_appointments WHERE conflict IS NOT NULL ORDER BY source,external_id LIMIT 100')]

    def resolve(self,source,external_id,version,expected_conflict,decision,reason,actor):
        validate_source(source)
        if decision not in ('apply','keep_local') or not 1<=len(reason.strip())<=500:
            raise QueueError('Укажите решение и причину',422)
        with acting_as(actor), self.queue.connection(write=True) as con:
            row=con.execute('SELECT * FROM mis_appointments WHERE source=? AND external_id=?',
                            (source,external_id)).fetchone()
            if not row or not row['conflict'] or row['version']!=version or row['conflict']!=expected_conflict:
                raise QueueError('Конфликт изменился; обновите список',409)
            ticket_id=row['ticket_id']
            if decision=='apply':
                if row['conflict']=='version_collision':
                    raise QueueError('Нужна новая однозначная версия МИС или решение keep_local',409)
                item=AppointmentSnapshot.model_validate_json(row['snapshot'])
                ticket_id,conflict=self._apply_snapshot(con,item,ticket_id)
                if conflict:
                    raise QueueError('Автоматическое применение по-прежнему небезопасно: '+conflict,409)
            con.execute('UPDATE mis_appointments SET ticket_id=?,conflict=NULL WHERE source=? AND external_id=?',
                        (ticket_id,source,external_id))
            audit(con,'mis_resolve','appointment',external_id,{'source':source,'version':version,
                  'decision':decision,'reason':reason.strip(),'previous':expected_conflict})
            return {'outcome':'resolved','decision':decision}

    def status(self):
        with self.queue.connection() as con:
            return {'cursors':[dict(r) for r in con.execute('SELECT * FROM mis_cursors ORDER BY source')],
                    'appointments':con.execute('SELECT COUNT(*) FROM mis_appointments').fetchone()[0],
                    'conflicts':con.execute('SELECT COUNT(*) FROM mis_appointments WHERE conflict IS NOT NULL').fetchone()[0],
                    'inbox':con.execute('SELECT COUNT(*) FROM mis_inbox').fetchone()[0],
                    'outbox':{r['state']:r['n'] for r in con.execute('SELECT state,COUNT(*) AS n FROM mis_outbox GROUP BY state')},
                    'dead_letters':[dict(r) for r in con.execute("""SELECT event_id,source,external_id,sequence,attempts,last_error
                        FROM mis_outbox WHERE state='dead' ORDER BY sequence LIMIT 100""")]}
