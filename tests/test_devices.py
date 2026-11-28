"""QR lifecycle, device schema and kiosk supervisor checks."""
import base64
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import time

import pytest
from PIL import Image
from fastapi.testclient import TestClient

from app.auth.store import acting_as
from app.domain.queue_policy import QueueError
from app.devices.qr import QRService,TTL
from app.queue import Queue,PostgresQueue
from app.main import create_app
from deploy.kiosk.kiosk_supervisor import prepare_url,Watchdog


@pytest.fixture(params=['sqlite','postgres'])
def queue(request,tmp_path):
    config={'database_url':request.getfixturevalue('postgres_url')} if request.param=='postgres' else {'db_path':tmp_path/'queue.db'}
    q=PostgresQueue(config['database_url']) if request.param=='postgres' else Queue(config['db_path'])
    q.initialize()
    try:
        yield q,config
    finally:
        q.close()


def test_qr_issue_and_atomic_redeem(queue):
    q,_=queue
    clock=[1000]
    service=QRService(q,clock=lambda:clock[0])
    first=service.issue('DEMO-101')
    second=service.issue('DEMO-101')
    assert first['qr_text']!=second['qr_text']
    assert second['expires_at']==1000+TTL and second['qr_text'].startswith('CF1:')
    image=Image.open(BytesIO(base64.b64decode(second['png_base64'])))
    assert image.format=='PNG' and image.width>=200 and image.height==image.width
    assert 'DEMO-101' not in second['qr_text']
    with pytest.raises(QueueError):
        service.redeem(first['qr_text'],'request-key-000001')
    def redeem():
        with acting_as('device:kiosk-1'):
            return service.redeem(second['qr_text'],'request-key-000002')
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:redeem(),range(4)))
    assert len({r['id'] for r in results})==1
    assert results[0]['status']=='waiting'
    assert len(q.history(results[0]['id'])['legs'])==1
    with acting_as('device:kiosk-2'),pytest.raises(QueueError):
        service.redeem(second['qr_text'],'request-key-000002')
    with acting_as('device:kiosk-1'),pytest.raises(QueueError):
        service.redeem(second['qr_text'],'request-key-000003')
    with q.connection() as con:
        row=con.execute('SELECT * FROM qr_credentials').fetchone()
        assert row['token_hash']!=second['qr_text'] and len(row['token_hash'])==64
        assert con.execute("SELECT COUNT(*) FROM events WHERE action='check_in'").fetchone()[0]==1
        assert con.execute("SELECT COUNT(*) FROM audit_events WHERE action='qr_redeem'").fetchone()[0]==1
    with pytest.raises(QueueError):
        service.issue('DEMO-101')


def test_qr_expiry_manual_checkin_and_failure_rollback(queue,monkeypatch):
    q,_=queue
    clock=[1000]
    service=QRService(q,clock=lambda:clock[0])
    a=service.issue('DEMO-101');clock[0]+=TTL
    with pytest.raises(QueueError):
        service.redeem(a['qr_text'],'expiry-request-001')
    b=service.issue('DEMO-102')
    q.check_in('DEMO-102')
    with pytest.raises(QueueError):
        service.redeem(b['qr_text'],'manual-request-001')
    c=service.issue('DEMO-103')
    original=q._event
    def broken(*args):
        original(*args)
        raise RuntimeError('test crash')
    monkeypatch.setattr(q,'_event',broken)
    with pytest.raises(RuntimeError):
        service.redeem(c['qr_text'],'crash-request-0001')
    monkeypatch.setattr(q,'_event',original)
    assert service.redeem(c['qr_text'],'crash-request-0001')['status']=='waiting'


def test_qr_http_legacy_profile(queue):
    _,config=queue
    with TestClient(create_app(**config,staff_password='test-only')) as c:
        assert c.post('/api/staff/qr',json={'booking_code':'DEMO-101'}).status_code==401
        data=c.post('/api/staff/qr',json={'booking_code':'DEMO-101'},auth=('staff','test-only')).json()
        body={'code':data['qr_text'],'request_key':'http-request-00001'}
        r=c.post('/api/check-in/qr',json=body)
        assert r.status_code==200 and r.json()['status']=='waiting'
        assert c.post('/api/check-in/qr',json=body).json()==r.json()
        assert c.post('/api/check-in/qr',json={**body,'request_key':'http-request-00002'}).status_code==409
        assert c.post('/api/check-in/qr',json={'code':'bad','request_key':'http-request-00001'}).status_code==422


def test_kiosk_urls_and_recovery():
    url,health=prepare_url('https://clinic.example:8443/board?room=101',True)
    assert url=='https://clinic.example:8443/board?room=101&device=1&audio=1'
    assert health=='https://clinic.example:8443/health'
    assert prepare_url('http://127.0.0.1:8000/kiosk')[0].endswith('/kiosk?device=1')
    for bad in ('http://remote.example/board','https://user:pw@clinic.example/board',
                'https://clinic.example/admin','https://clinic.example/board?token=secret',
                'https://clinic.example/kiosk?room=101','https://clinic.example/board?room=bad%2Froom'):
        with pytest.raises(ValueError):
            prepare_url(bad)
    w=Watchdog()
    assert w.update(False,False)=='wait'
    assert w.update(True,False)=='start'
    assert w.update(False,True)=='wait'
    assert w.update(True,True)=='wait'
    assert w.update(False,True)=='wait'
    assert w.update(False,True)=='wait'
    assert w.update(True,True)=='restart'
    assert w.update(True,True)=='wait'
