"""Capture linked queue mutations and deliver them at least once.

No network calls inside a queue transaction. Stable event IDs are persisted
with the domain event. Later events for an appointment wait behind unresolved
earlier deliveries, including dead letters.
"""
import json
import secrets
import time
from uuid import uuid4

from app.auth.store import audit
from app.domain.queue_policy import QueueError, now
from .contracts import canonical_json

LOCAL_EVENTS={'check_in','call','repeat','start','finish','no_show','cancel','requeue','transfer'}
MAX_ATTEMPTS=5


def enqueue(con, sequence, ticket_id, action, status, ts):
    if action not in LOCAL_EVENTS:
        return
    row=con.execute('''SELECT a.source,a.external_id,a.version,t.room_id
        FROM mis_appointments a JOIN tickets t ON a.ticket_id=t.id WHERE t.id=?''',(ticket_id,)).fetchone()
    if not row:
        return
    event_id=str(uuid4())
    payload={'event_id':event_id,'sequence':sequence,'external_id':row['external_id'],
             'appointment_version':row['version'],'action':action,'queue_status':status,
             'room_id':row['room_id'],'occurred_at':ts}
    con.execute('''INSERT INTO mis_outbox(event_id,sequence,source,external_id,payload,state,available_at)
        VALUES (?,?,?,?,?,'pending',?)''',(event_id,sequence,row['source'],row['external_id'],
        canonical_json(payload),int(time.time())))


class Outbox:
    def __init__(self,queue,clock=time.time,lease_seconds=60):
        self.queue=queue
        self.clock=clock
        self.lease_seconds=lease_seconds

    def claim(self,source):
        ts=int(self.clock())
        with self.queue.connection(write=True) as con:
            # A crash on the final attempt must not leave the event stuck forever.
            con.execute("""UPDATE mis_outbox SET state='dead',lease_until=NULL,lease_token=NULL,last_error='lease_expired'
                WHERE source=? AND state='inflight' AND lease_until<=? AND attempts>=?""",(source,ts,MAX_ATTEMPTS))
            row=con.execute("""SELECT o.* FROM mis_outbox o WHERE o.source=?
                AND ((o.state='pending' AND o.available_at<=?) OR (o.state='inflight' AND o.lease_until<=?))
                AND o.attempts<?
                AND NOT EXISTS (SELECT 1 FROM mis_outbox p WHERE p.source=o.source AND p.external_id=o.external_id
                    AND p.sequence<o.sequence AND p.state!='delivered')
                ORDER BY o.sequence LIMIT 1""",(source,ts,ts,MAX_ATTEMPTS)).fetchone()
            if not row:
                return None
            lease=secrets.token_urlsafe(24)
            con.execute("UPDATE mis_outbox SET state='inflight',attempts=attempts+1,lease_until=?,lease_token=? WHERE event_id=?",
                        (ts+self.lease_seconds,lease,row['event_id']))
            return {'event_id':row['event_id'],'lease':lease,'payload':json.loads(row['payload']),
                    'attempts':row['attempts']+1}

    def settle(self,claim,success):
        ts=int(self.clock())
        with self.queue.connection(write=True) as con:
            row=con.execute("SELECT attempts FROM mis_outbox WHERE event_id=? AND state='inflight' AND lease_token=?",
                            (claim['event_id'],claim['lease'])).fetchone()
            if not row:
                return False
            if success:
                con.execute("""UPDATE mis_outbox SET state='delivered',delivered_at=?,lease_token=NULL,lease_until=NULL,last_error=NULL
                    WHERE event_id=?""",(now(),claim['event_id']))
            else:
                dead=row['attempts']>=MAX_ATTEMPTS
                con.execute("""UPDATE mis_outbox SET state=?,available_at=?,lease_token=NULL,lease_until=NULL,last_error=?
                    WHERE event_id=?""",('dead' if dead else 'pending',ts+min(300,2**row['attempts']),
                    'delivery_failed',claim['event_id']))
            return True

    def retry_dead(self,event_id,actor):
        with self.queue.connection(write=True) as con:
            row=con.execute('SELECT state FROM mis_outbox WHERE event_id=?',(event_id,)).fetchone()
            if not row or row['state']!='dead':
                raise QueueError('Событие не найдено или не в dead-letter',409)
            con.execute("""UPDATE mis_outbox SET state='pending',attempts=0,available_at=?,lease_until=NULL,
                lease_token=NULL,last_error=NULL WHERE event_id=?""",(int(self.clock()),event_id))
            audit(con,'outbox_retry','mis_event',event_id,actor=actor)
            return {'event_id':event_id,'state':'pending'}
