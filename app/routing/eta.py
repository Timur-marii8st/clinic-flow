"""Simple non-ML queue workload estimate, conditional on observed service times.

Not a promised appointment time: future arrivals, staffing, clinical priority,
breaks, and scheduling uncertainty are not predicted.
"""
from datetime import datetime
from math import ceil
from statistics import median

from app.domain.queue_policy import eligible, now, seconds, sort_key

MIN_SAMPLES=5

def percentile(values,q):
    if not values:return None
    return round(sorted(values)[max(0,ceil(q*len(values))-1)],1)


def estimate(con,room,ts=None):
    """Estimate a newly eligible, non-priority arrival behind current eligible work."""
    ts=ts or now()
    legs=con.execute("""SELECT started_at,ended_at FROM queue_legs
        WHERE room_id=? AND outcome='done' AND started_at IS NOT NULL AND ended_at IS NOT NULL
        ORDER BY ended_at DESC,id DESC LIMIT 100""",(room,)).fetchall()
    durations=[seconds(r['started_at'],r['ended_at']) for r in legs]
    # Drop invalid/zero observations; a completed synthetic instant call does not
    # supply a meaningful service-time baseline.
    samples=[v for v in durations if v is not None and v>0]
    rows=con.execute("SELECT * FROM tickets WHERE room_id=? AND status IN ('waiting','called','serving')",(room,)).fetchall()
    waiting=[r for r in rows if eligible(r,ts)]
    active=next((r for r in rows if r['status'] in ('called','serving')),None)
    workload=len(waiting)+(1 if active else 0)
    result={'room_id':room,'eligible_waiting':len(waiting),'active':bool(active),
            'workload':workload,'samples':len(samples),'eta_seconds':None,
            'service_median_seconds':None,'method':'insufficient_data'}
    if len(samples)<MIN_SAMPLES:
        return result
    typical=max(1.0,median(samples))
    remaining=0.0
    if active:
        remaining=typical
        if active['status']=='serving':
            leg=con.execute('SELECT started_at FROM queue_legs WHERE ticket_id=? AND ended_at IS NULL',(active['id'],)).fetchone()
            if leg and leg['started_at']:
                remaining=max(0.0,typical-(seconds(leg['started_at'],ts) or 0))
    result.update(eta_seconds=round(remaining+len(waiting)*typical,1),
                  service_median_seconds=round(typical,1),method='median_workload')
    return result


def rank_choices(con,steps,allowed=None):
    candidates=[]
    cached={}
    for step in steps:
        for room in step.rooms:
            if allowed is not None and room not in allowed:continue
            if room not in cached:cached[room]=estimate(con,room)
            candidates.append({'step_id':step.id,'step_label':step.label,**cached[room]})
    # Never prefer a room just because it has data: use one comparable rule.
    method='eta' if candidates and all(c['eta_seconds'] is not None for c in candidates) else 'workload'
    candidates.sort(key=lambda c:(c['eta_seconds'] if method=='eta' else c['workload'],c['step_id'],c['room_id']))
    for i,c in enumerate(candidates):
        c['rank_method']=method
        c['recommended']=i==0
    return candidates
