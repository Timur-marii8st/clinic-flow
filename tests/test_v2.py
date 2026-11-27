from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient
from app.main import Changes, create_app
from app.queue import Queue, QueueError


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / 'v2.sqlite3', staff_password='')) as c:
        yield c


def issue(client, room='101', key=None):
    body = {'room_id': room}
    if key:
        body['request_key'] = key
    r = client.post('/api/walk-in', json=body)
    assert r.status_code == 201, r.text
    return r.json()


def action(client, ticket, action, version=None):
    return client.post(f"/api/staff/{ticket['room_id']}/tickets/{ticket['id']}/action",
                       json={'action': action, 'expected_version': version})


def test_transfer_history_and_isolation(client):
    t = issue(client)
    assert client.post('/api/staff/101/call-next', json={}).json()['id'] == t['id']
    assert action(client, t, 'start').status_code == 200
    r = client.post(f"/api/staff/101/tickets/{t['id']}/transfer", json={'target_room':'102'})
    assert r.status_code == 200
    moved = r.json()
    assert moved['code'] == t['code'] and moved['status'] == 'waiting'
    assert client.get('/api/staff/101').json() == []
    assert client.get('/api/staff/102').json()[0]['id'] == t['id']
    assert client.get('/api/board').json()[0]['state'] == 'idle'
    assert action(client,t,'cancel').status_code == 404
    legs = client.get(f"/api/staff/tickets/{t['id']}/history").json()['legs']
    assert [l['room_id'] for l in legs] == ['101','102']
    assert legs[0]['outcome'] == 'transferred' and legs[0]['started_at'] and legs[0]['ended_at']
    assert legs[1]['ended_at'] is None
    assert client.post('/api/staff/102/call-next',json={}).json()['id'] == moved['id']
    assert action(client,moved,'start').status_code == 200
    assert action(client,moved,'finish').status_code == 200
    legs = client.get(f"/api/staff/tickets/{t['id']}/history").json()['legs']
    assert [l['outcome'] for l in legs] == ['transferred','done']


def test_idempotent_walk_in_concurrent(tmp_path):
    q = Queue(tmp_path / 'idempotent.db'); q.initialize()
    with ThreadPoolExecutor(max_workers=8) as pool:
        tickets = list(pool.map(lambda _: q.walk_in('101','same-request-key-123'), range(8)))
    assert len({t['id'] for t in tickets}) == 1
    assert len(q.staff_view('101')) == 1
    with pytest.raises(QueueError):
        q.walk_in('102','same-request-key-123')
    q.transfer('101', tickets[0]['id'], '102')
    assert q.walk_in('101','same-request-key-123')['room_id'] == '102'


def test_requeue_and_stale_version(client):
    t = issue(client)
    version = client.get('/api/staff/101').json()[0]['version']
    client.post('/api/staff/101/call-next',json={})
    assert action(client,t,'start',version).status_code == 409
    assert action(client,t,'no_show').status_code == 200
    missed = client.get('/api/staff/101/no-shows').json()
    assert missed[0]['id'] == t['id']
    assert action(client,t,'requeue',missed[0]['version']).json()['status'] == 'waiting'
    assert client.get('/api/staff/101/no-shows').json() == []
    assert action(client,t,'requeue').status_code == 409
    legs = client.get(f"/api/staff/tickets/{t['id']}/history").json()['legs']
    assert [l['outcome'] for l in legs] == ['no_show',None]


def test_scheduled_slots_and_repeat_import(client):
    now = datetime.now(timezone.utc)
    early = {'booking_code':'DEMO-FUTURE','room_id':'101','scheduled_at':(now+timedelta(hours=1)).isoformat()}
    due = {'booking_code':'DEMO-DUE','room_id':'101','scheduled_at':(now-timedelta(minutes=1)).isoformat()}
    walk = issue(client)
    for booking in (early,due):
        r = client.post('/api/staff/demo-appointments',json=booking)
        assert r.status_code == 201, r.text
        assert client.post('/api/staff/demo-appointments',json=booking).json()['id'] == r.json()['id']
        assert client.post('/api/check-in',json={'booking_code':booking['booking_code']}).status_code == 200
    tickets = client.get('/api/staff/101').json()
    assert tickets[0]['source'] == 'appointment' and tickets[0]['eligible']
    assert tickets[-1]['eligible'] is False
    t = client.post('/api/staff/101/call-next',json={}).json()
    assert t['id'] == tickets[0]['id']
    assert client.post(f"/api/staff/101/tickets/{t['id']}/transfer",json={'target_room':'102'}).status_code == 200
    assert client.post('/api/staff/demo-appointments',json=due).json()['id'] == t['id']
    assert client.post('/api/staff/101/call-next',json={}).json()['id'] == walk['id']
    assert action(client,walk,'start').status_code == 200
    assert action(client,walk,'finish').status_code == 200
    assert client.post('/api/staff/101/call-next',json={}).status_code == 409
    assert client.post('/api/staff/demo-appointments',json={**early,'booking_code':'DEMO-NAIVE','scheduled_at':'2026-11-26T12:00:00'}).status_code == 422


def test_aging_prevents_walkin_starvation(tmp_path):
    q = Queue(tmp_path / 'aging.db'); q.initialize()
    old = q.walk_in('101')
    due = q.book_demo('DEMO-DUE','101',datetime.now(timezone.utc)-timedelta(minutes=1))
    q.check_in('DEMO-DUE')
    with q.connection(write=True) as con:
        con.execute('UPDATE tickets SET checked_in_at=? WHERE id=?', ((datetime.now(timezone.utc)-timedelta(minutes=35)).isoformat(), old['id']))
    assert q.staff_view('101')[0]['id'] == old['id']
    assert q.call_next('101')['id'] == old['id'] != due['id']


def test_analytics_and_public_fields(client):
    for _ in range(3):
        t = issue(client)
        client.post('/api/staff/101/call-next',json={})
        board = client.get('/api/board').json()[0]
        assert set(board) == {'room_id','label','code','state','call_version'}
        action(client,t,'start'); action(client,t,'finish')
    a = client.get('/api/analytics').json()[0]
    assert a['completed'] == 3 and a['service_samples'] == 3 and a['wait_samples'] == 3
    assert a['median_service_seconds'] >= 0 and a['median_wait_seconds'] >= 0
    assert client.get('/api/analytics').json()[1]['median_service_seconds'] is None


def test_staff_auth_and_origin(tmp_path):
    with TestClient(create_app(tmp_path / 'auth.db', staff_password='local-test-only')) as c:
        assert c.get('/board').status_code == 200
        assert c.get('/staff').status_code == 401
        assert c.get('/analytics').status_code == 401
        assert c.get('/api/staff/101').status_code == 401
        assert c.get('/api/analytics').status_code == 401
        assert c.post('/api/staff/101/call-next',json={}).status_code == 401
        t = issue(c)
        assert c.get('/api/staff/101', auth=('staff','wrong')).status_code == 401
        assert c.get('/api/staff/101', auth=('staff','local-test-only')).json()[0]['id'] == t['id']
        assert c.post('/api/staff/101/call-next',json={},auth=('staff','local-test-only'),headers={'Origin':'https://evil.example'}).status_code == 403
        assert c.post('/api/staff/101/call-next',json={},auth=('staff','local-test-only')).status_code == 200
        assert c.post('/api/walk-in',data={'room_id':'101'}).status_code == 403


def test_change_signal_and_heartbeat():
    async def run():
        hub = Changes()
        waiter = asyncio.create_task(hub.wait(0,1))
        await hub.publish()
        assert await waiter == 1
        assert await hub.wait(1,0.01) == 1
        await hub.publish()
        assert await hub.wait(1,0.01) == 2
    asyncio.run(run())


def test_v1_database_migration(tmp_path):
    path = tmp_path / 'v1.db'
    with sqlite3.connect(path) as con:
        con.executescript('''
        CREATE TABLE rooms(id TEXT PRIMARY KEY,label TEXT NOT NULL);
        CREATE TABLE tickets(id INTEGER PRIMARY KEY AUTOINCREMENT,room_id TEXT NOT NULL REFERENCES rooms(id),source TEXT NOT NULL,booking_code TEXT UNIQUE,status TEXT NOT NULL,created_at TEXT NOT NULL,checked_in_at TEXT,updated_at TEXT NOT NULL);
        CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,ticket_id INTEGER NOT NULL REFERENCES tickets(id),action TEXT NOT NULL,old_status TEXT,new_status TEXT NOT NULL,created_at TEXT NOT NULL);
        INSERT INTO rooms VALUES ('101','Кабинет 101');
        INSERT INTO tickets VALUES (1,'101','walk_in',NULL,'done','2026-11-26T09:00:00.000000+00:00','2026-11-26T09:00:00.000000+00:00','2026-11-26T09:15:00.000000+00:00');
        INSERT INTO events(ticket_id,action,old_status,new_status,created_at) VALUES (1,'call','waiting','called','2026-11-26T09:04:00.000000+00:00'),(1,'start','called','serving','2026-11-26T09:05:00.000000+00:00');
        ''')
    q = Queue(path); q.initialize(); q.initialize()
    legs = q.history(1)['legs']
    assert len(legs) == 1 and legs[0]['outcome'] == 'done'
    assert q.analytics()[0]['median_wait_seconds'] == 300
    assert q.analytics()[0]['service_samples'] == 1
    assert q.walk_in('101')['status'] == 'waiting'
