from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
import pytest
from app.main import create_app
from app.queue import Queue, QueueError


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / 'test.sqlite3')) as c:
        yield c


def test_demo_flow(client):
    assert client.get('/health').json()['status'] == 'ok'
    assert len(client.get('/api/rooms').json()) == 3
    ticket = client.post('/api/check-in', json={'booking_code':'DEMO-101'}).json()
    assert ticket['status'] == 'waiting'
    assert client.post('/api/check-in', json={'booking_code':'demo-101'}).json() == ticket
    other = client.post('/api/walk-in', json={'room_id':'101'}).json()
    assert client.post('/api/staff/101/call-next', json={}).json()['id'] == ticket['id']
    board = client.get('/api/board').json()[0]
    assert board['code'] == ticket['code'] and 'booking_code' not in board
    assert client.post('/api/staff/101/call-next', json={}).status_code == 409
    url = f"/api/staff/101/tickets/{ticket['id']}/action"
    assert client.post(url, json={'action':'finish'}).status_code == 409
    assert client.post(url, json={'action':'repeat'}).json()['status'] == 'called'
    assert client.post(url, json={'action':'start'}).json()['status'] == 'serving'
    assert client.get('/api/board').json()[0]['code'] is None
    assert client.post(url, json={'action':'finish'}).json()['status'] == 'done'
    assert client.post('/api/staff/101/call-next', json={}).json()['id'] == other['id']


def test_invalid_and_room_isolation(client):
    assert client.post('/api/check-in', json={'booking_code':'missing'}).status_code == 404
    assert client.post('/api/walk-in', json={'room_id':'999'}).status_code == 404
    assert client.post('/api/staff/103/call-next', json={}).status_code == 409
    ticket = client.post('/api/walk-in', json={'room_id':'102'}).json()
    assert client.post(f"/api/staff/101/tickets/{ticket['id']}/action", json={'action':'cancel'}).status_code == 404
    assert not client.get('/api/staff/101').json()
    assert client.get('/api/staff/102').json()[0]['id'] == ticket['id']


def test_no_show_and_cancel(client):
    a = client.post('/api/walk-in', json={'room_id':'101'}).json()
    b = client.post('/api/walk-in', json={'room_id':'101'}).json()
    client.post('/api/staff/101/call-next', json={})
    assert client.post(f"/api/staff/101/tickets/{a['id']}/action", json={'action':'no_show'}).json()['status'] == 'no_show'
    assert client.post(f"/api/staff/101/tickets/{b['id']}/action", json={'action':'cancel'}).json()['status'] == 'cancelled'
    assert not client.get('/api/staff/101').json()


def test_concurrent_call_and_persistence(tmp_path):
    path = tmp_path / 'queue.sqlite3'
    q = Queue(path)
    q.initialize()
    q.walk_in('101'); q.walk_in('101')
    def call():
        try:
            return q.call_next('101')['status']
        except QueueError:
            return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: call(), range(2))) == ['called','conflict']
    reopened = Queue(path); reopened.initialize()
    assert [t['status'] for t in reopened.staff_view('101')] == ['called','waiting']
    with reopened.connection() as con:
        assert con.execute("SELECT COUNT(*) FROM events WHERE action='seed'").fetchone()[0] == 3
        assert con.execute("SELECT COUNT(*) FROM events WHERE action='call'").fetchone()[0] == 1


def test_ui_and_api_schema(client):
    for route in ('/', '/kiosk', '/staff', '/board', '/static/app.js', '/static/style.css', '/openapi.json'):
        r = client.get(route)
        assert r.status_code == 200
        assert r.headers['cache-control'] == 'no-store'
