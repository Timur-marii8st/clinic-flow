"""Route templates and explicit stage advancement; no automatic clinical decisions."""
import json
import re
from app.auth.store import audit
from app.domain.queue_policy import QueueError, now
from .plan import Plan
from .state import active_run,get_run,state_of,summary
from .eta import rank_choices


class Routing:
    def __init__(self,queue):
        self.queue=queue

    def create_template(self,template_id,label,plan,expected_version):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,32}',template_id) or not 1<=len(label.strip())<=100:
            raise QueueError('Некорректный шаблон',422)
        plan=Plan.model_validate(plan)
        with self.queue.connection(write=True) as con:
            for room in {r for step in plan.steps for r in step.rooms}:
                self.queue._room(con,room)
            latest=con.execute('SELECT COALESCE(MAX(version),0) AS v FROM route_templates WHERE id=?',(template_id,)).fetchone()['v']
            if expected_version!=latest:
                raise QueueError('Версия шаблона изменилась; обновите список',409)
            version=latest+1
            con.execute('INSERT INTO route_templates VALUES (?,?,?,?,?)',
                        (template_id,version,label.strip(),plan.model_dump_json(),now()))
            audit(con,'route_template_create','route_template',template_id,{'version':version})
            return {'id':template_id,'version':version,'label':label.strip(),'plan':plan.model_dump()}

    def templates(self):
        with self.queue.connection() as con:
            return [{'id':r['id'],'version':r['version'],'label':r['label'],'plan':json.loads(r['plan'])}
                    for r in con.execute("""SELECT t.* FROM route_templates t WHERE version=
                    (SELECT MAX(n.version) FROM route_templates n WHERE n.id=t.id) ORDER BY t.id LIMIT 100""")]

    def attach(self,room,ticket_id,template_id,version,step_id,expected_version,key=None):
        def execute(con):
            row=con.execute('SELECT * FROM tickets WHERE id=? AND room_id=?',(ticket_id,room)).fetchone()
            if not row:raise QueueError('Талон не найден',404)
            self.queue._verify_version(row,expected_version)
            if row['status']!='waiting' or get_run(con,ticket_id):
                raise QueueError('Маршрут можно назначить только ожидающему талону без маршрута',409)
            template=con.execute('SELECT * FROM route_templates WHERE id=? AND version=?',(template_id,version)).fetchone()
            if not template:raise QueueError('Шаблон не найден',404)
            plan=Plan.model_validate_json(template['plan'])
            step=next((s for s in plan.ready([]) if s.id==step_id and room in s.rooms),None)
            if not step:raise QueueError('Первый этап не разрешён для этого кабинета',409)
            ts=now()
            con.execute("""INSERT INTO visit_routes(ticket_id,template_id,template_version,active_step,completed,state,attached_at)
                VALUES (?,?,?,?,'[]','active',?)""",(ticket_id,template_id,version,step_id,ts))
            con.execute('UPDATE tickets SET updated_at=? WHERE id=?',(ts,ticket_id))
            self.queue._event(con,ticket_id,'route_attach','waiting','waiting',ts)
            audit(con,'route_attach_detail','ticket',ticket_id,{'template':template_id,'version':version,'step':step_id})
            return summary(get_run(con,ticket_id))
        return self.queue._command('route_attach',key,[room,ticket_id,template_id,version,step_id,expected_version],execute)

    def options(self,room,ticket_id,allowed=None):
        with self.queue.connection() as con:
            row=con.execute('SELECT * FROM tickets WHERE id=? AND room_id=?',(ticket_id,room)).fetchone()
            if not row:raise QueueError('Талон не найден',404)
            run=active_run(con,ticket_id)
            if not run:raise QueueError('Активный маршрут не найден',404)
            plan,completed=state_of(run)
            done=completed+[run['active_step']]
            return {'route':summary(run),'version':row['updated_at'],
                    'final':len(done)==len(plan.steps),'choices':rank_choices(con,plan.ready(done),allowed)
                    if row['status']=='serving' else []}

    def advance(self,room,ticket_id,step_id,target_room,expected_version,key=None):
        def execute(con):
            row=con.execute('SELECT * FROM tickets WHERE id=? AND room_id=?',(ticket_id,room)).fetchone()
            if not row:raise QueueError('Талон не найден',404)
            self.queue._verify_version(row,expected_version)
            run=active_run(con,ticket_id)
            if not run or row['status']!='serving':
                raise QueueError('Сначала начните текущий этап',409)
            plan,completed=state_of(run)
            completed.append(run['active_step'])
            step=next((s for s in plan.ready(completed) if s.id==step_id and target_room in s.rooms),None)
            if not step:raise QueueError('Следующий этап или кабинет не разрешён',409)
            self.queue._room(con,target_room)
            ts=now()
            # An actually completed procedure is a done leg, not a transfer-only leg.
            self.queue._end_leg(con,ticket_id,ts,'done')
            self.queue._new_leg(con,ticket_id,target_room,ts)
            con.execute("""UPDATE visit_routes SET completed=?,active_step=? WHERE ticket_id=?""",
                        (json.dumps(completed),step_id,ticket_id))
            con.execute("""UPDATE tickets SET room_id=?,status='waiting',checked_in_at=?,updated_at=?,scheduled_at=NULL
                WHERE id=?""",(target_room,ts,ts,ticket_id))
            # Existing MIS contract sees a transfer, not a final visit completion.
            self.queue._event(con,ticket_id,'transfer','serving','waiting',ts)
            audit(con,'route_advance','ticket',ticket_id,{'completed_step':run['active_step'],'next_step':step_id,
                  'from_room':room,'to_room':target_room})
            return self.queue._public(self.queue._ticket(con,ticket_id))
        return self.queue._command('route_advance',key,[room,ticket_id,step_id,target_room,expected_version],execute)
