"""Security contract: real RSA signature, fake OIDC HTTP provider, both stores."""
import base64
import hashlib
import json
import time
from urllib.parse import parse_qs,urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.auth.http import SESSION,DEVICE,STATE
from app.auth.oidc import OIDCClient,SecuritySettings
from app.main import create_app

SETTINGS=SecuritySettings('https://clinic.example','https://idp.example/realms/clinic',
                          'clinic-flow','synthetic-secret','admin-sub')
ORIGIN={'Origin':SETTINGS.public_origin}


class Provider:
    def __init__(self):
        self.key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        self.jwk=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        self.jwk.update({'kid':'key-1','alg':'RS256','use':'sig'})
        self.pending={}
        self.counter=0
        self.transport=httpx.MockTransport(self.handle)

    def handle(self,request):
        path=request.url.path
        issuer=SETTINGS.issuer
        if path.endswith('/.well-known/openid-configuration'):
            return httpx.Response(200,json={'issuer':issuer,'authorization_endpoint':issuer+'/auth',
                'token_endpoint':issuer+'/token','jwks_uri':issuer+'/keys',
                'id_token_signing_alg_values_supported':['RS256'],'code_challenge_methods_supported':['S256']})
        if path.endswith('/keys'):
            return httpx.Response(200,json={'keys':[self.jwk]})
        if path.endswith('/token'):
            form=parse_qs(request.content.decode())
            code=form['code'][0]
            if code not in self.pending:
                return httpx.Response(400,json={'error':'invalid_grant'})
            challenge,claims=self.pending.pop(code)
            assert request.headers['authorization']=='Basic '+base64.b64encode(b'clinic-flow:synthetic-secret').decode()
            assert form['redirect_uri']==[SETTINGS.callback_url]
            assert form['grant_type']==['authorization_code']
            actual=base64.urlsafe_b64encode(hashlib.sha256(form['code_verifier'][0].encode()).digest()).decode().rstrip('=')
            assert actual==challenge
            signed=jwt.encode(claims,self.key,algorithm='RS256',headers={'kid':'key-1'})
            return httpx.Response(200,json={'id_token':signed,'access_token':'never-store-this','refresh_token':'never-store-that'})
        return httpx.Response(404)

    def prepare(self,redirect,subject,**overrides):
        values=parse_qs(urlsplit(redirect).query)
        assert values['code_challenge_method']==['S256']
        assert values['scope']==['openid']
        self.counter+=1
        code='synthetic-code-'+str(self.counter)
        claims={'iss':SETTINGS.issuer,'sub':subject,'aud':SETTINGS.client_id,
                'iat':int(time.time()),'exp':int(time.time())+120,'nonce':values['nonce'][0],**overrides}
        self.pending[code]=(values['code_challenge'][0],claims)
        return {'state':values['state'][0],'code':code}


def login(client,provider,subject,**overrides):
    response=client.get('/auth/login?next=/admin',follow_redirects=False)
    assert response.status_code==303
    assert 'HttpOnly' in response.headers['set-cookie'] and 'Secure' in response.headers['set-cookie']
    assert STATE in client.cookies
    params=provider.prepare(response.headers['location'],subject,**overrides)
    return client.get('/auth/callback',params=params,follow_redirects=False)


def post(client,path,body,**headers):
    return client.post(path,json=body,headers={**ORIGIN,**headers})


def save_user(client,subject,role,rooms=(),enabled=True):
    response=post(client,'/api/admin/users',{'subject':subject,'label':subject,'role':role,'rooms':list(rooms),'enabled':enabled})
    assert response.status_code==200,response.text


@pytest.fixture(params=['sqlite','postgres'])
def secure(request,tmp_path):
    config={'db_path':tmp_path/'secure.db'} if request.param=='sqlite' else {'database_url':request.getfixturevalue('postgres_url')}
    provider=Provider()
    app=create_app(**config,security_settings=SETTINGS,oidc_client=OIDCClient(SETTINGS,provider.transport),staff_password='ignored')
    with TestClient(app,base_url=SETTINGS.public_origin) as client:
        yield client,provider


def test_secure_fail_closed_oidc_and_replay(secure):
    c,p=secure
    assert c.get('/api/board').status_code==401
    assert c.get('/api/staff/101',auth=('staff','ignored')).status_code==401
    assert c.get('/staff',follow_redirects=False).status_code==303
    assert c.get('/board',follow_redirects=False).headers['location']=='/pair'
    assert c.get('/openapi.json').status_code==404
    for claims in ({'iss':'https://other.example'},{'aud':'another-client'},{'nonce':'wrong'},
                   {'exp':int(time.time())-100},{'azp':'other-client'}):
        assert login(c,p,'admin-sub',**claims).status_code==401
        assert SESSION not in c.cookies
    assert login(c,p,'unknown-sub').status_code==401
    response=c.get('/auth/login?next=https://evil.example',follow_redirects=False)
    params=p.prepare(response.headers['location'],'admin-sub')
    assert c.get('/auth/callback',params={**params,'state':'wrong'},follow_redirects=False).status_code==401
    response=c.get('/auth/login?next=https://evil.example',follow_redirects=False)
    params=p.prepare(response.headers['location'],'admin-sub')
    original=c.cookies[STATE]
    response=c.get('/auth/callback',params=params,follow_redirects=False)
    assert response.status_code==303 and response.headers['location']=='/staff'
    assert SESSION in c.cookies and STATE not in c.cookies
    c.cookies.set(STATE,original,domain='clinic.example',path='/')
    assert c.get('/auth/callback',params=params,follow_redirects=False).status_code==401
    assert c.get('/api/me').json()['role']=='admin'
    assert c.post('/api/staff/rooms',json={'room_id':'A','label':'A'}).status_code==403
    assert c.post('/api/staff/rooms',json={'room_id':'A','label':'A'},headers={'Origin':'https://evil.example'}).status_code==403
    assert post(c,'/api/staff/rooms',{'room_id':'A','label':'A'}).status_code==200
    cookie=c.cookies[SESSION]
    assert post(c,'/auth/logout',{}).status_code==200
    c.cookies.set(SESSION,cookie,domain='clinic.example',path='/')
    assert c.get('/api/me').status_code==401


def test_room_roles_and_audit(secure):
    c,p=secure
    assert login(c,p,'admin-sub').status_code==303
    admin=c.cookies[SESSION]
    save_user(c,'doctor-sub','doctor',['101'])
    save_user(c,'analyst-sub','analyst')
    save_user(c,'registrar-sub','registrar')
    first=post(c,'/api/walk-in',{'room_id':'101'}).json()
    second=post(c,'/api/walk-in',{'room_id':'102'}).json()
    c.cookies.clear()
    assert login(c,p,'doctor-sub').status_code==303
    doctor=c.cookies[SESSION]
    assert [r['id'] for r in c.get('/api/rooms').json()]==['101']
    assert [r['room_id'] for r in c.get('/api/board').json()]==['101']
    assert c.get('/api/staff/102').status_code==403
    assert c.get('/api/staff/102/no-shows').status_code==403
    assert c.get('/api/admin/users').status_code==403
    assert c.get('/api/integration/status').status_code==403
    assert post(c,'/api/staff/qr',{'booking_code':'DEMO-101'}).status_code==403
    assert c.post('/api/integration/retry',json={},headers=ORIGIN).status_code==403
    assert c.get('/api/analytics').status_code==403
    assert c.get('/api/staff/tickets/'+str(second['id'])+'/history').status_code==403
    assert post(c,'/api/walk-in',{'room_id':'101'}).status_code==403
    assert post(c,'/api/staff/rooms',{'room_id':'X','label':'X'}).status_code==403
    assert post(c,'/api/staff/102/call-next',{}).status_code==403
    call=post(c,'/api/staff/101/call-next',{},**{'Idempotency-Key':'unique-call-key-0001'})
    assert call.json()['id']==first['id']
    assert post(c,'/api/staff/101/call-next',{},**{'Idempotency-Key':'unique-call-key-0001'}).json()==call.json()
    assert post(c,f"/api/staff/101/tickets/{first['id']}/transfer",{'target_room':'102'}).status_code==403
    assert post(c,f"/api/staff/101/tickets/{first['id']}/action",{'action':'finish'}).status_code==409
    assert post(c,f"/api/staff/101/tickets/{first['id']}/action",{'action':'start'}).status_code==200
    c.cookies.clear()
    assert login(c,p,'analyst-sub').status_code==303
    assert c.get('/api/analytics').status_code==200
    assert c.get('/api/staff/101').status_code==403
    assert c.get('/api/board').status_code==403
    c.cookies.clear()
    assert login(c,p,'registrar-sub').status_code==303
    assert post(c,f"/api/staff/101/tickets/{first['id']}/transfer",{'target_room':'102'}).status_code==200
    c.cookies.clear()
    c.cookies.set(SESSION,admin,domain='clinic.example',path='/')
    assert c.get('/api/integration/status').status_code==200
    assert c.get('/api/integration/conflicts').json()==[]
    audit=c.get('/api/admin/audit').json()
    entries=[x for x in audit if x['actor']=='staff:doctor-sub' and x['object_id']==str(first['id'])]
    assert {x['action'] for x in entries}=={'call','start'}
    assert len(entries)==2
    assert not any('never-store-this' in json.dumps(a) for a in audit)
    save_user(c,'doctor-sub','doctor',['101'],enabled=False)
    c.cookies.clear()
    c.cookies.set(SESSION,doctor,domain='clinic.example',path='/')
    assert c.get('/api/me').status_code==401
    assert login(c,p,'doctor-sub').status_code==401


def test_device_pairing_scope_heartbeat_revoke(secure):
    c,p=secure
    assert login(c,p,'admin-sub').status_code==303
    admin=c.cookies[SESSION]
    qr=post(c,'/api/staff/qr',{'booking_code':'DEMO-101'}).json()['qr_text']
    qr_body={'code':qr,'request_key':'secure-qr-request-001'}
    board=post(c,'/api/admin/devices',{'label':'Board 101','kind':'board','room_id':'101'}).json()
    kiosk=post(c,'/api/admin/devices',{'label':'Kiosk','kind':'kiosk'}).json()
    assert 'pairing_code' not in c.get('/api/admin/devices').text
    c.cookies.clear()
    assert post(c,'/auth/pair',{'pairing_code':board['pairing_code']}).json()['path']=='/board'
    board_cookie=c.cookies[DEVICE]
    assert c.get('/api/me').json()['role']=='board'
    assert c.get('/board').status_code==200
    assert [r['room_id'] for r in c.get('/api/board').json()]==['101']
    assert c.get('/api/staff/101').status_code==403
    assert post(c,'/api/walk-in',{'room_id':'101'}).status_code==403
    assert post(c,'/api/devices/heartbeat',{}).status_code==200
    telemetry={'page':'board','state':'ready','age_seconds':3,'audio_ready':True,'client_version':'0.6.0'}
    assert post(c,'/api/devices/heartbeat',telemetry).status_code==200
    assert post(c,'/api/devices/heartbeat',{**telemetry,'page':'kiosk'}).status_code==422
    assert post(c,'/api/devices/heartbeat',{**telemetry,'patient':'forbidden'}).status_code==422
    assert post(c,'/api/check-in/qr',qr_body).status_code==403
    assert post(c,'/api/staff/qr',{'booking_code':'DEMO-101'}).status_code==403
    c.cookies.clear()
    assert post(c,'/auth/pair',{'pairing_code':board['pairing_code']}).status_code==403
    assert post(c,'/auth/pair',{'pairing_code':kiosk['pairing_code']}).json()['path']=='/kiosk'
    assert c.get('/api/board').status_code==403
    assert post(c,'/api/check-in/qr',qr_body).status_code==200
    assert post(c,'/api/check-in/qr',qr_body).status_code==200
    assert post(c,'/api/walk-in',{'room_id':'102'}).status_code==201
    assert post(c,'/api/staff/102/call-next',{}).status_code==403
    c.cookies.clear()
    c.cookies.set(SESSION,admin,domain='clinic.example',path='/')
    info=next(x for x in c.get('/api/admin/devices').json() if x['id']==board['id'])
    assert info['display_state']=='ready' and info['audio_ready'] and info['age_seconds']==3 and info['client_version']=='0.6.0'
    assert post(c,'/api/admin/devices/'+board['id']+'/revoke',{}).status_code==200
    devices=c.get('/api/admin/devices').json()
    assert next(x for x in devices if x['id']==board['id'])['status']=='revoked'
    c.cookies.clear()
    c.cookies.set(DEVICE,board_cookie,domain='clinic.example',path='/')
    assert c.get('/api/board').status_code==401
    assert post(c,'/api/devices/heartbeat',{}).status_code==401


def test_secure_config_rejects_http_and_missing_credentials(monkeypatch):
    monkeypatch.setenv('CLINIC_MODE','secure')
    for key in ('CLINIC_PUBLIC_ORIGIN','CLINIC_OIDC_ISSUER','CLINIC_OIDC_CLIENT_ID','CLINIC_OIDC_CLIENT_SECRET','CLINIC_BOOTSTRAP_SUBJECT'):
        monkeypatch.delenv(key,raising=False)
    with pytest.raises(ValueError):
        SecuritySettings.from_env()
    with pytest.raises(ValueError):
        SecuritySettings('http://clinic.example',SETTINGS.issuer,'client','secret','sub')

def test_route_permissions(secure):
    c,p=secure
    assert login(c,p,'admin-sub').status_code==303
    admin=c.cookies[SESSION]
    save_user(c,'route-doctor','doctor',['101'])
    body={'id':'test','label':'Test','expected_version':0,'plan':{'steps':[
        {'id':'start','label':'Start','rooms':['101']},
        {'id':'end','label':'End','rooms':['102'],'requires':['start']}]}}
    assert post(c,'/api/routes/templates',body).status_code==201
    t=post(c,'/api/walk-in',{'room_id':'101'}).json()
    tid=t['id'];url=f'/api/staff/101/tickets/{tid}/route'
    ver=c.get('/api/staff/101').json()[0]['version']
    assert post(c,url+'/attach',{'template_id':'test','template_version':1,'step_id':'start','expected_version':ver}).status_code==200
    assert c.get('/metrics').status_code==404
    c.cookies.clear()
    assert login(c,p,'route-doctor').status_code==303
    assert c.get('/api/routes/templates').status_code==403
    assert post(c,'/api/routes/templates',body).status_code==403
    assert c.get('/api/pilot/analytics').status_code==403
    post(c,'/api/staff/101/call-next',{})
    post(c,f'/api/staff/101/tickets/{tid}/action',{'action':'start'})
    opts=c.get(url).json()
    assert opts['choices']==[] and not opts['final']
    assert post(c,url+'/advance',{'step_id':'end','target_room':'102','expected_version':opts['version']}).status_code==403
    assert post(c,f'/api/staff/101/tickets/{tid}/action',{'action':'finish'}).status_code==409
    c.cookies.clear()
    c.cookies.set(SESSION,admin,domain='clinic.example',path='/')
    assert c.get(url).json()['choices'][0]['room_id']=='102'
    assert post(c,url+'/advance',{'step_id':'end','target_room':'102','expected_version':opts['version']}).status_code==200
