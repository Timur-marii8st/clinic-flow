from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.queue import PostgresQueue, Queue, QueueError


@pytest.fixture(params=['sqlite', 'postgres'])
def store(request, tmp_path):
    if request.param == 'postgres':
        url = request.getfixturevalue('postgres_url')
        queue = PostgresQueue(url)
        make = lambda: PostgresQueue(url)
    else:
        path = tmp_path / 'v3.sqlite3'
        queue = Queue(path)
        make = lambda: Queue(path)
    queue.initialize()
    try:
        yield queue, make
    finally:
        queue.close()


def test_room_config_persistence_and_validation(store):
    queue, make = store
    assert queue.upsert_room('LAB-1', ' Лаборатория ') == {'id':'LAB-1','label':'Лаборатория'}
    queue.upsert_room('101', 'Терапевт')
    with pytest.raises(QueueError):
        queue.upsert_room('bad/room', 'Bad')
    with pytest.raises(QueueError):
        queue.upsert_room('LAB-1', ' ')
    t = queue.walk_in('LAB-1')
    assert queue.call_next('LAB-1')['id'] == t['id']
    other = make()
    try:
        other.initialize()
        rooms = {r['id']: r['label'] for r in other.rooms()}
        assert rooms['101'] == 'Терапевт' and rooms['LAB-1'] == 'Лаборатория'
        assert other.staff_view('LAB-1')[0]['status'] == 'called'
    finally:
        other.close()


def test_call_idempotency_after_finish_and_conflict(store):
    q, make = store
    first = q.walk_in('101')
    second = q.walk_in('101')
    key = 'call-key-demo-0001'
    called = q.call_next('101', key)
    assert called['id'] == first['id']
    q.act('101', first['id'], 'start')
    q.act('101', first['id'], 'finish')
    other = make()
    try:
        assert other.call_next('101', key) == called
        assert other.staff_view('101')[0]['id'] == second['id']
        assert other.staff_view('101')[0]['status'] == 'waiting'
        with pytest.raises(QueueError):
            other.call_next('102', key)
        assert other.call_next('101','call-key-demo-0002')['id'] == second['id']
    finally:
        other.close()


def test_concurrent_across_two_stores(store):
    q, make = store
    q.walk_in('101'); q.walk_in('101')
    other = make()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = list(pool.map(lambda x: x.call_next('101', 'shared-concurrent-key'), (q,other)))
        assert calls[0] == calls[1]
        assert [x['status'] for x in q.staff_view('101')] == ['called','waiting']
        with q.connection() as con:
            assert con.execute("SELECT COUNT(*) FROM events WHERE action='call'").fetchone()[0] == 1
        q.act('101',calls[0]['id'],'start')
        q.act('101',calls[0]['id'],'finish')
        q.walk_in('101')
        def call(pair):
            obj, key = pair
            try:
                return obj.call_next('101', key)['status']
            except QueueError:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(call, ((q,'different-key-0001'),(other,'different-key-0002')))) == ['called','conflict']
    finally:
        other.close()


def test_action_transfer_replay_and_rollback(store):
    q, _ = store
    t = q.walk_in('101', 'walk-key-unique-01')
    q.call_next('101')
    version = q.staff_view('101')[0]['version']
    with pytest.raises(QueueError):
        q.act('101', t['id'], 'finish', version, 'failed-command-0001')
    assert q.act('101',t['id'],'start',version,'failed-command-0001')['status'] == 'serving'
    # A rejected transaction must not consume the command key.
    assert q.act('101',t['id'],'start',version,'failed-command-0001')['status'] == 'serving'
    serving_version = q.staff_view('101')[0]['version']
    moved = q.transfer('101',t['id'],'102',serving_version,'transfer-key-00001')
    assert moved['room_id']=='102'
    assert q.transfer('101',t['id'],'102',serving_version,'transfer-key-00001') == moved
    with pytest.raises(QueueError):
        q.transfer('101',t['id'],'103',serving_version,'transfer-key-00001')
    assert len(q.history(t['id'])['legs']) == 2
    assert q.walk_in('101','walk-key-unique-01')['id'] == t['id']


def test_schedule_analytics_and_board_across_stores(store):
    q, _ = store
    ts = datetime.now(timezone.utc)
    future = q.book_demo('DEMO-NEW-FUTURE','103',ts+timedelta(hours=1))
    due = q.book_demo('DEMO-NEW-DUE','103',ts-timedelta(minutes=1))
    q.check_in('DEMO-NEW-FUTURE'); q.check_in('DEMO-NEW-DUE')
    assert q.call_next('103')['id'] == due['id']
    assert q.board()[2]['code'] == due['code']
    q.act('103',due['id'],'start')
    q.act('103',due['id'],'finish')
    assert q.staff_view('103')[0]['id'] == future['id']
    with pytest.raises(QueueError):
        q.call_next('103')
    assert q.analytics()[2]['completed'] == 1


@pytest.mark.parametrize('backend', ['sqlite','postgres'])
def test_api_v3(backend, tmp_path, request):
    options = {'database_url':request.getfixturevalue('postgres_url')} if backend=='postgres' else {'db_path':tmp_path/'api.db'}
    with TestClient(create_app(staff_password='synthetic-test-password',**options)) as c:
        auth = ('staff','synthetic-test-password')
        assert c.get('/health').json()['version'] == '0.6.0'
        assert c.post('/api/staff/rooms',json={'room_id':'LAB','label':'Лаборатория'}).status_code==401
        assert c.post('/api/staff/rooms',json={'room_id':'LAB','label':'Лаборатория'},auth=auth).status_code==200
        assert c.post('/api/staff/rooms',json={'room_id':'bad/slash','label':'Test'},auth=auth).status_code==422
        first = c.post('/api/walk-in',json={'room_id':'LAB'}).json()
        path = '/api/staff/LAB/call-next'
        assert c.post(path,json={},headers={'Idempotency-Key':'short'},auth=auth).status_code==422
        headers = {'Idempotency-Key':'api-replay-demo-0001'}
        assert c.post(path,json={},headers=headers,auth=auth).json()['id']==first['id']
        assert c.post(path,json={},headers=headers,auth=auth).json()['id']==first['id']
        assert c.post('/api/staff/101/call-next',json={},headers=headers,auth=auth).status_code==409
