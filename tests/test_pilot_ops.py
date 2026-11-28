"""Synthetic SQLite recovery and privacy of Prometheus exposition."""
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from app.queue import Queue
from app.main import create_app
from app.routing.service import Routing
from scripts.demo_backup import sqlite_copy, dbname


def test_sqlite_backup_and_restore(tmp_path):
    source=tmp_path/'source.db'; backup=tmp_path/'backup.db'; restored=tmp_path/'restored.db'
    q=Queue(source);q.initialize()
    t=q.walk_in('101')
    r=Routing(q)
    r.create_template('test','Test',{'steps':[{'id':'one','label':'Test','rooms':['101']}]},0)
    version=q.staff_view('101')[0]['version']
    r.attach('101',t['id'],'test',1,'one',version)
    sqlite_copy(source,backup)
    q.call_next('101')
    sqlite_copy(backup,restored)
    copied=Queue(restored);copied.initialize()
    assert copied.staff_view('101')[0]['status']=='waiting'
    assert copied.staff_view('101')[0]['route']['template_id']=='test'
    assert q.staff_view('101')[0]['status']=='called'
    with pytest.raises(FileExistsError):sqlite_copy(backup,restored)
    assert backup.stat().st_mode & 0o077==0
    assert dbname('demo_restore')=='demo_restore'
    with pytest.raises(ValueError):dbname('postgresql://password@host/clinic')


def test_metrics_privacy_and_token(tmp_path,monkeypatch):
    monkeypatch.setenv('CLINIC_METRICS_TOKEN','synthetic-monitoring-token')
    with TestClient(create_app(tmp_path/'metric.db',staff_password='')) as c:
        c.post('/api/walk-in',json={'room_id':'101','request_key':'secret-request-00001'})
        c.get('/api/staff/SECRET-PATH')
        assert c.get('/metrics').status_code==401
        result=c.get('/metrics',headers={'Authorization':'Bearer synthetic-monitoring-token'})
        assert result.status_code==200
        assert 'clinic_db_up 1.0' in result.text
        assert 'clinic_tickets{state="waiting"} 1.0' in result.text
        assert 'clinic_http_requests_total' in result.text
        assert 'SECRET-PATH' not in result.text and 'secret-request' not in result.text
