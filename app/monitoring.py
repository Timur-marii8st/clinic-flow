"""Prometheus metrics with bounded labels, no patient/operator/device identifiers."""
import hmac
import os
import time
from fastapi import Request, HTTPException
from fastapi.responses import Response
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from prometheus_client.core import GaugeMetricFamily

TICKET_STATES=('booked','waiting','called','serving','done','no_show','cancelled')
OUTBOX_STATES=('pending','inflight','delivered','dead')
ROUTE_STATES=('active','complete','cancelled')


class Metrics:
    def __init__(self,queue,secure=False):
        self.queue=queue
        self.secure=secure
        self.token=os.environ.get('CLINIC_METRICS_TOKEN','')
        self.registry=CollectorRegistry()
        self.requests=Counter('clinic_http_requests_total','HTTP responses by method, route family and status',
                              ['method','family','status'],registry=self.registry)
        self.latency=Histogram('clinic_http_response_seconds','Time to response headers, not SSE stream lifetime',
                              ['family'],registry=self.registry,buckets=(.01,.025,.05,.1,.2,.5,1,2,5,10))
        self.registry.register(self)

    @staticmethod
    def family(path):
        # No raw paths or query strings (even for 404s). Cardinality is fixed.
        if path.startswith('/api/staff/'):return 'staff'
        if path.startswith('/api/admin/'):return 'admin'
        if path.startswith('/api/integration/'):return 'integration'
        if path.startswith('/api/routes/'):return 'routes'
        if path.startswith('/api/devices/'):return 'devices'
        if path in ('/api/analytics','/api/pilot/analytics'):return 'analytics'
        if path in ('/api/check-in','/api/check-in/qr','/api/walk-in'):return 'registration'
        if path=='/api/board':return 'board'
        if path=='/api/events':return 'events'
        if path.startswith('/auth/'):return 'auth'
        if path.startswith('/static/'):return 'static'
        if path=='/health':return 'health'
        return 'other'

    def collect(self):
        try:
            with self.queue.connection() as con:
                data=[]
                for name,table,states in (
                    ('clinic_tickets','tickets',TICKET_STATES),
                    ('clinic_mis_outbox','mis_outbox',OUTBOX_STATES),
                    ('clinic_routes','visit_routes',ROUTE_STATES)):
                    counts={r['state']:r['n'] for r in con.execute(
                        f"SELECT {('status' if table=='tickets' else 'state')} AS state, COUNT(*) AS n FROM {table} GROUP BY {('status' if table=='tickets' else 'state')}")}
                    data.append((name,states,counts))
                conflicts=con.execute('SELECT COUNT(*) FROM mis_appointments WHERE conflict IS NOT NULL').fetchone()[0]
                devices=con.execute('SELECT COUNT(*) FROM devices WHERE revoked=0 AND token_expires>? AND last_seen>?',(int(time.time()),int(time.time())-75)).fetchone()[0]
            yield GaugeMetricFamily('clinic_db_up','Queue database reachable',value=1)
            for name,states,counts in data:
                m=GaugeMetricFamily(name,'Current aggregate records by state',labels=['state'])
                for state in states:m.add_metric([state],counts.get(state,0))
                yield m
            yield GaugeMetricFamily('clinic_mis_conflicts','Open MIS conflicts',value=conflicts)
            yield GaugeMetricFamily('clinic_devices_online','Devices with recent heartbeat',value=devices)
        except Exception:
            yield GaugeMetricFamily('clinic_db_up','Queue database reachable',value=0)

    def install(self,app):
        @app.middleware('http')
        async def observe(request,call_next):
            started=time.perf_counter()
            response=None
            try:
                response=await call_next(request)
                return response
            finally:
                if request.url.path!='/metrics':
                    family=self.family(request.url.path)
                    method=request.method if request.method in ('GET','POST','HEAD','OPTIONS') else 'OTHER'
                    self.requests.labels(method,family,str(response.status_code if response else 500)).inc()
                    self.latency.labels(family).observe(time.perf_counter()-started)

        @app.get('/metrics',include_in_schema=False)
        def scrape(request:Request):
            if self.secure and not self.token:
                raise HTTPException(404,'Metrics disabled')
            if self.token and not hmac.compare_digest(request.headers.get('authorization','').encode(),('Bearer '+self.token).encode()):
                raise HTTPException(401,'Monitoring credential required')
            return Response(generate_latest(self.registry),media_type=CONTENT_TYPE_LATEST,
                            headers={'Cache-Control':'no-store'})
