from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pydantic import ValidationError
import pytest
from fastapi.testclient import TestClient

from app.queue import Queue,PostgresQueue,QueueError
from app.main import create_app
from app.routing.plan import Plan
from app.routing.service import Routing
from app.routing.report import flow_report
from app.routing.eta import estimate
from app.routing.simulation import compare
from app.domain.queue_policy import utc_iso

PLAN={'steps':[
    {'id':'start','label':'Вход','rooms':['101']},
    {'id':'a','label':'A','rooms':['102','103'],'requires':['start']},
    {'id':'b','label':'B','rooms':['103'],'requires':['start']},
    {'id':'end','label':'Итог','rooms':['101'],'requires':['a','b']}]}


@pytest.fixture(params=['sqlite','postgres'])
def setup(request,tmp_path):
    cfg={'database_url':request.getfixturevalue('postgres_url')} if request.param=='postgres' else {'db_path':tmp_path/'route.db'}
    q=PostgresQueue(cfg['database_url']) if request.param=='postgres' else Queue(cfg['db_path'])
    q.initialize()
    try:yield q,Routing(q),cfg
    finally:q.close()


def version(q,room,tid):
    return next(t['version'] for t in q.staff_view(room) if t['id']==tid)


def call_start(q,room,tid):
    assert q.call_next(room)['id']==tid
    q.act(room,tid,'start')


def attach(q,r):
    r.create_template('demo','Демонстрационный маршрут',PLAN,0)
    t=q.walk_in('101')
    r.attach('101',t['id'],'demo',1,'start',version(q,'101',t['id']))
    return t['id']


def test_plan_validation_and_simulation():
    for invalid in (
        {'steps':[{'id':'x','label':'x','rooms':['101'],'requires':['x']}]},
        {'steps':[{'id':'x','label':'x','rooms':['101'],'requires':['missing']}]},
        {'steps':[{'id':'x','label':'x','rooms':['101','101']}]},
        {'steps':[{'id':'x','label':'x','rooms':['101']},{'id':'x','label':'x','rooms':['102']}]}
    ):
        with pytest.raises(ValidationError):Plan.model_validate(invalid)
    plan=Plan.model_validate(PLAN)
    a=compare(plan,count=20,seed=7)
    assert a==compare(plan,count=20,seed=7)
    for result in a['results']:
        assert result['patients']==20 and result['stage_samples']==80
        assert result['visit_p90_seconds']>=result['visit_p50_seconds']>=0
        assert sum(r['stages'] for r in result['rooms'].values())==80


def test_route_complete_and_bypass_guards(setup):
    q,r,_=setup;tid=attach(q,r)
    q.walk_in('102')
    call_start(q,'101',tid)
    options=r.options('101',tid)
    assert not options['final']
    assert {c['step_id'] for c in options['choices']}=={'a','b'}
    assert options['choices'][0]['room_id']=='103'
    assert options['choices'][0]['rank_method']=='workload'
    assert all(c['eta_seconds'] is None for c in options['choices'])
    assert {c['room_id'] for c in r.options('101',tid,{'102'})['choices']}=={'102'}
    with pytest.raises(QueueError):q.act('101',tid,'finish')
    with pytest.raises(QueueError):q.transfer('101',tid,'102')
    with pytest.raises(QueueError):r.advance('101',tid,'end','101',options['version'])
    result=r.advance('101',tid,'b','103',options['version'],'advance-retry-key-001')
    assert result['room_id']=='103'
    assert r.advance('101',tid,'b','103',options['version'],'advance-retry-key-001')==result
    call_start(q,'103',tid)
    opt=r.options('103',tid)
    assert {c['step_id'] for c in opt['choices']}=={'a'}
    r.advance('103',tid,'a','103',opt['version'])
    call_start(q,'103',tid)
    opt=r.options('103',tid)
    assert {c['step_id'] for c in opt['choices']}=={'end'}
    r.advance('103',tid,'end','101',opt['version'])
    call_start(q,'101',tid)
    assert r.options('101',tid)['final']
    q.act('101',tid,'finish')
    assert q.history(tid)['ticket']['status']=='done'
    assert [l['outcome'] for l in q.history(tid)['legs']]==['done']*4
    report=flow_report(q)
    assert report['routes']['completed_sample']==1 and report['routes']['active']==0
    assert sum(room['completed'] for room in report['rooms'])==4


def test_version_and_cancel_or_no_show(setup):
    q,r,_=setup;tid=attach(q,r)
    newer={'steps':[{'id':'only','label':'Новый','rooms':['101']}]}
    assert r.create_template('demo','New',newer,1)['version']==2
    with pytest.raises(QueueError):r.create_template('demo','Conflict',newer,1)
    assert q.staff_view('101')[0]['route']['template_version']==1
    q.call_next('101');q.act('101',tid,'no_show')
    q.act('101',tid,'requeue')
    assert q.staff_view('101')[0]['route']['active_step']=='start'
    q.act('101',tid,'cancel')
    assert flow_report(q)['routes']['cancelled_in_window']==1
    with pytest.raises(QueueError):r.options('101',tid)
    with pytest.raises(QueueError):r.create_template('unknown','Bad',{'steps':[{'id':'x','label':'x','rooms':['missing']}]},0)


def test_eta_baseline_and_future_slots(setup):
    q,r,_=setup
    current=datetime.now(timezone.utc)
    for n in range(5):
        t=q.walk_in('102')['id']
        call_start(q,'102',t);q.act('102',t,'finish')
        end=utc_iso(current-timedelta(minutes=2+n*5))
        start=utc_iso(current-timedelta(minutes=4+n*5))
        with q.connection(write=True) as con:
            con.execute('UPDATE queue_legs SET entered_at=?,started_at=?,ended_at=? WHERE ticket_id=?',(start,start,end,t))
    q.walk_in('102')
    q.book_demo('DEMO-FUTURE-ETA','102',current+timedelta(days=1));q.check_in('DEMO-FUTURE-ETA')
    with q.connection() as con:
        result=estimate(con,'102')
        assert result['samples']==5 and result['eta_seconds']==120 and result['eligible_waiting']==1
    report=flow_report(q)
    row=next(x for x in report['rooms'] if x['room_id']=='102')
    assert row['completed']==5 and row['service_p90_seconds']==120


def test_concurrent_route_advance(setup):
    q,r,_=setup;tid=attach(q,r);call_start(q,'101',tid)
    ver=version(q,'101',tid)
    def advance(room):
        try:return r.advance('101',tid,'a',room,ver)['room_id']
        except QueueError:return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(advance,['102','103']))
    assert results.count('conflict')==1
    assert len(q.history(tid)['legs'])==2


def test_route_api_demo_authorization(setup):
    q,r,cfg=setup
    with TestClient(create_app(**cfg,staff_password='test-only')) as c:
        auth=('staff','test-only')
        assert c.get('/api/routes/templates').status_code==401
        assert c.post('/api/routes/templates',json={'id':'demo','label':'Route','expected_version':0,'plan':PLAN},auth=auth).status_code==201
        ticket=c.post('/api/walk-in',json={'room_id':'101'}).json()
        tid=ticket['id'];v=version(q,'101',tid)
        body={'template_id':'demo','template_version':1,'step_id':'start','expected_version':v}
        assert c.post(f'/api/staff/101/tickets/{tid}/route/attach',json=body,auth=auth).status_code==200
        assert c.get('/api/pilot/analytics?hours=24',auth=auth).json()['routes']['active']==1
        assert c.get('/api/pilot/analytics?hours=0',auth=auth).status_code==422
