"""Contract, durable feed, reconciliation and failure-injection tests on both stores."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.domain.queue_policy import QueueError
from app.integrations.contracts import AppointmentEvent,AppointmentSnapshot
from app.integrations.mock import MockMISAdapter
from app.integrations.outbox import Outbox,MAX_ATTEMPTS
from app.integrations.service import Integration
from app.integrations.worker import Worker
from app.main import create_app
from app.queue import Queue,PostgresQueue

SLOT=datetime(2026,1,1,10,tzinfo=timezone.utc)


def event(version=1,room='101',state='booked',external='appointment-1',
          code='MIS-DEMO-0001',slot=SLOT,event_id=None):
    return AppointmentEvent(event_id=event_id or f'{external}-v{version}',
        appointment=AppointmentSnapshot(external_id=external,version=version,
            booking_code=code,room_id=room,scheduled_at=slot,state=state))


@pytest.fixture(params=['sqlite','postgres'])
def system(request,tmp_path):
    if request.param=='postgres':
        url=request.getfixturevalue('postgres_url')
        make=lambda: PostgresQueue(url)
        config={'database_url':url}
    else:
        path=tmp_path/'queue.db'
        make=lambda: Queue(path)
        config={'db_path':path}
    q=make();q.initialize()
    mock=MockMISAdapter(tmp_path/'provider.db')
    try:
        yield q,make,mock,config
    finally:
        q.close()


def test_contract_rejects_bad_data():
    base=event().appointment.model_dump()
    for patch in ({'version':0},{'version':'1'},{'scheduled_at':datetime(2026,1,1)},
                  {'booking_code':'REAL-123'},{'patient_name':'not-allowed'}):
        with pytest.raises(ValidationError):
            AppointmentSnapshot.model_validate({**base,**patch})
    with pytest.raises(ValidationError):
        AppointmentEvent(event_id='bad event',appointment=event().appointment)


def test_import_reschedule_cancel_and_stale(system):
    q,make,mock,_=system
    integ=Integration(q);worker=Worker(q,mock)
    assert mock.publish(event())==1
    assert mock.publish(event())==1
    assert worker.cycle()['pulled']==1
    assert integ.cursor('mock')==1
    with q.connection() as con:
        first=con.execute("SELECT id FROM tickets WHERE booking_code='MIS-DEMO-0001'").fetchone()['id']
    mock.publish(event(3,'102',slot=SLOT+timedelta(hours=1)))
    mock.publish(event(2,'103'))
    assert worker.cycle()['pulled']==2
    assert integ.cursor('mock')==3
    with q.connection() as con:
        row=con.execute('SELECT * FROM tickets WHERE id=?',(first,)).fetchone()
        assert row['room_id']=='102' and row['scheduled_at'].startswith('2026-01-01T11')
        assert con.execute("SELECT outcome FROM mis_inbox WHERE event_id='appointment-1-v2'").fetchone()['outcome']=='stale'
    mock.publish(event(4,'102','cancelled'))
    assert worker.cycle()['pulled']==1
    assert q.history(first)['ticket']['status']=='cancelled'
    with pytest.raises(QueueError):
        q.check_in('MIS-DEMO-0001')
    assert integ.status()['outbox']=={}
    other=make()
    try:
        other.initialize()
        assert Worker(other,mock).cycle()['pulled']==0
        assert Integration(other).cursor('mock')==4
    finally:
        other.close()


def test_arrived_conflict_does_not_move_patient(system):
    q,_,mock,_=system
    i=Integration(q);w=Worker(q,mock)
    mock.publish(event());w.cycle()
    t=q.check_in('MIS-DEMO-0001')
    q.call_next('101');q.act('101',t['id'],'start')
    mock.publish(event(2,'102','cancelled'))
    w.cycle()
    assert q.history(t['id'])['ticket']['status']=='serving'
    assert q.history(t['id'])['ticket']['room_id']=='101'
    conflict=i.conflicts()[0]
    assert conflict['conflict']=='visit_started'
    assert 'booking_code' not in conflict['desired']
    with pytest.raises(QueueError):
        i.resolve('mock','appointment-1',2,'visit_started','apply','Нельзя прерывать приём','staff:admin')
    with pytest.raises(QueueError):
        i.resolve('mock','appointment-1',1,'visit_started','keep_local','Старое решение','staff:admin')
    assert i.resolve('mock','appointment-1',2,'visit_started','keep_local','Приём уже идёт','staff:admin')['outcome']=='resolved'
    q.act('101',t['id'],'finish')
    w.cycle()
    assert [x['action'] for x in mock.received()]==['check_in','call','start','finish']
    assert [x['appointment_version'] for x in mock.received()]==[1,1,1,2]
    assert i.conflicts()==[]


def test_conflicts_unknown_room_code_and_version(system):
    q,_,mock,_=system
    i=Integration(q)
    assert i.apply('mock',event(room='NEW'))['outcome']=='conflict'
    c=i.conflicts()[0];assert c['conflict']=='unknown_room' and c['ticket_id'] is None
    q.upsert_room('NEW','New')
    assert i.resolve('mock','appointment-1',1,'unknown_room','apply','Кабинет создан','staff:admin')['outcome']=='resolved'
    assert i.apply('mock',event(room='NEW'))['outcome']=='duplicate'
    with pytest.raises(QueueError):
        i.apply('mock',event(room='101'))
    assert i.apply('mock',event(room='101',event_id='different-id'))['outcome']=='version_collision'
    with pytest.raises(QueueError):
        q.check_in('MIS-DEMO-0001')
    with pytest.raises(QueueError):
        i.resolve('mock','appointment-1',1,'version_collision','apply','Ambiguous','staff:admin')
    assert i.apply('mock',event(2,room='NEW'))['outcome']=='applied'
    assert i.apply('mock',event(external='appointment-2',room='NEW'))['outcome']=='conflict'
    assert i.conflicts()[0]['conflict']=='booking_code_collision'
    t=q.check_in('MIS-DEMO-0001')
    assert t['room_id']=='NEW'


def test_cursor_and_inbox_atomic_on_failure(system,monkeypatch):
    q,_,mock,_=system
    i=Integration(q);w=Worker(q,mock)
    mock.publish(event())
    original=q._event
    def fail(*args):
        original(*args)
        raise RuntimeError('injected')
    monkeypatch.setattr(q,'_event',fail)
    assert w.cycle()['pull_error']
    assert i.cursor('mock')==0 and i.status()['inbox']==0 and i.status()['appointments']==0
    with q.connection() as con:
        assert con.execute("SELECT COUNT(*) FROM tickets WHERE booking_code='MIS-DEMO-0001'").fetchone()[0]==0
    monkeypatch.setattr(q,'_event',original)
    assert w.cycle()['pulled']==1 and i.cursor('mock')==1
    with pytest.raises(QueueError):
        i.apply('mock',event(2),3)
    assert i.cursor('mock')==1 and i.status()['inbox']==1
    assert i.apply('mock',event(2),2)['outcome']=='applied'


def test_outbox_commit_order_and_duplicate_delivery(system):
    q,make,mock,_=system
    Integration(q).apply('mock',event())
    t=q.check_in('MIS-DEMO-0001')
    q.call_next('101','test-call-key-00001')
    q.call_next('101','test-call-key-00001')
    with pytest.raises(QueueError):
        q.act('101',t['id'],'finish')
    assert Integration(q).status()['outbox']=={'pending':2}
    clock=[int(time.time())+5]
    box=Outbox(q,clock=lambda:clock[0],lease_seconds=3)
    other=make()
    try:
        other.initialize()
        otherbox=Outbox(other,clock=lambda:clock[0],lease_seconds=3)
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims=list(pool.map(lambda b:b.claim('mock'),(box,otherbox)))
        assert sum(c is not None for c in claims)==1
        claim=next(c for c in claims if c)
        mock.deliver(claim['payload']) # provider commits, worker crashes before ack
        clock[0]+=4
        reclaimed=otherbox.claim('mock')
        assert reclaimed['event_id']==claim['event_id'] and reclaimed['attempts']==2
        assert not box.settle(claim,True) # stale lease cannot settle new claim
        mock.deliver(reclaimed['payload'])
        assert otherbox.settle(reclaimed,True)
        assert Worker(q,mock,box).cycle()['delivered']==1
        assert len(mock.received())==2
        assert [x['action'] for x in mock.received()]==['check_in','call']
        assert Integration(q).status()['outbox']=={'delivered':2}
    finally:
        other.close()


def test_dead_letter_retry_and_ordering(system):
    q,_,mock,_=system
    Integration(q).apply('mock',event())
    q.check_in('MIS-DEMO-0001');q.call_next('101')
    clock=[int(time.time())+10]
    box=Outbox(q,clock=lambda:clock[0],lease_seconds=2)
    for n in range(MAX_ATTEMPTS):
        claim=box.claim('mock')
        assert claim['attempts']==n+1
        if n<MAX_ATTEMPTS-1:
            assert box.settle(claim,False)
        # Last attempt crashes before acknowledgement.
        clock[0]+=400
    assert box.claim('mock') is None
    stat=Integration(q).status()
    assert stat['outbox']=={'dead':1,'pending':1}
    box.retry_dead(stat['dead_letters'][0]['event_id'],'staff:admin')
    assert Worker(q,mock,box).cycle()['delivered']==2
    assert [x['action'] for x in mock.received()]==['check_in','call']


def test_provider_failure_and_recovery(system):
    q,_,mock,_=system
    Integration(q).apply('mock',event())
    q.check_in('MIS-DEMO-0001')
    clock=[int(time.time())+10]
    box=Outbox(q,clock=lambda:clock[0])
    class Offline:
        source='mock'
        def pull(self,*args):raise OSError('synthetic outage')
        def deliver(self,*args):raise OSError('synthetic outage')
    result=Worker(q,Offline(),box).cycle()
    assert result['pull_error'] and result['failed']==1
    assert Integration(q).status()['outbox']=={'pending':1}
    clock[0]+=3
    assert Worker(q,mock,box).cycle()['delivered']==1
    assert mock.received()[0]['action']=='check_in'


def test_admin_api_and_legacy_auth(system):
    q,_,_,config=system
    Integration(q).apply('mock',event(room='ABSENT'))
    with TestClient(create_app(**config,staff_password='test-only')) as c:
        auth=('staff','test-only')
        assert c.get('/api/integration/status').status_code==401
        assert c.get('/api/integration/status',auth=auth).json()['conflicts']==1
        conflict=c.get('/api/integration/conflicts',auth=auth).json()[0]
        body={k:conflict[k] for k in ('source','external_id','version')}
        body.update(expected_conflict=conflict['conflict'],decision='keep_local',reason='Тест')
        assert c.post('/api/integration/resolve',auth=auth,json=body).status_code==200
        assert c.get('/api/integration/conflicts',auth=auth).json()==[]
