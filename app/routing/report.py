"""Bounded aggregate report: observed completed stages, not inferred utilization."""
from datetime import datetime,timedelta,timezone
from .eta import percentile,estimate
from app.domain.queue_policy import seconds,utc_iso

CAP=10000


def flow_report(queue,hours=24,at=None):
    if not 1<=hours<=168:raise ValueError('hours outside 1..168')
    end=at or datetime.now(timezone.utc)
    start=end-timedelta(hours=hours)
    lo,hi=utc_iso(start),utc_iso(end)
    with queue.connection() as con:
        rooms=[]
        for room in con.execute('SELECT id,label FROM rooms ORDER BY id').fetchall():
            rows=con.execute("""SELECT entered_at,started_at,ended_at,outcome FROM queue_legs
                WHERE room_id=? AND ended_at>=? AND ended_at<=? ORDER BY ended_at DESC,id DESC LIMIT ?""",
                (room['id'],lo,hi,CAP+1)).fetchall()
            truncated=len(rows)>CAP
            rows=rows[:CAP]
            done=[r for r in rows if r['outcome']=='done' and r['started_at']]
            waits=[seconds(r['entered_at'],r['started_at']) for r in done]
            durations=[seconds(r['started_at'],r['ended_at']) for r in done]
            ordered=sorted(done,key=lambda r:r['started_at'])
            gaps=[seconds(a['ended_at'],b['started_at']) for a,b in zip(ordered,ordered[1:]) if b['started_at']>=a['ended_at']]
            observed=sum(seconds(max(r['started_at'],lo),r['ended_at']) or 0 for r in done)
            rooms.append({'room_id':room['id'],'label':room['label'],'ended_sample':len(rows),
                         'truncated':truncated,'completed':len(done),'no_shows':sum(r['outcome']=='no_show' for r in rows),
                         'wait_samples':len(waits),'wait_p50_seconds':percentile(waits,.5),'wait_p90_seconds':percentile(waits,.9),
                         'service_p50_seconds':percentile(durations,.5),'service_p90_seconds':percentile(durations,.9),
                         'observed_service_seconds':round(observed,1),'gap_samples':len(gaps),
                         'between_logged_services_p50_seconds':percentile(gaps,.5),
                         'current_estimate':estimate(con,room['id'],hi)})
        completed=con.execute("""SELECT attached_at,ended_at FROM visit_routes WHERE state='complete'
            AND ended_at>=? AND ended_at<=? ORDER BY ended_at DESC LIMIT ?""",(lo,hi,CAP+1)).fetchall()
        route_times=[seconds(r['attached_at'],r['ended_at']) for r in completed[:CAP]]
        return {'window_start':lo,'window_end':hi,'rooms':rooms,
                'routes':{'completed_sample':len(route_times),'truncated':len(completed)>CAP,
                          'active':con.execute("SELECT COUNT(*) FROM visit_routes WHERE state='active'").fetchone()[0],
                          'cancelled_in_window':con.execute("SELECT COUNT(*) FROM visit_routes WHERE state='cancelled' AND ended_at>=? AND ended_at<=?",(lo,hi)).fetchone()[0],
                          'from_attachment_p50_seconds':percentile(route_times,.5),
                          'from_attachment_p90_seconds':percentile(route_times,.9)}}
