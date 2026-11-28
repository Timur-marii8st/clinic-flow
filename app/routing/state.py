"""SQL helpers shared by queue commands and the route service."""
import json
from app.domain.queue_policy import QueueError
from .plan import Plan


def get_run(con,ticket_id):
    return con.execute("""SELECT r.*,t.label,t.plan FROM visit_routes r JOIN route_templates t
        ON t.id=r.template_id AND t.version=r.template_version WHERE r.ticket_id=?""",(ticket_id,)).fetchone()


def active_run(con,ticket_id):
    run=get_run(con,ticket_id)
    return run if run and run['state']=='active' else None


def state_of(run):
    plan=Plan.model_validate_json(run['plan'])
    completed=json.loads(run['completed'])
    return plan,completed


def summary(run):
    if not run:return None
    plan,completed=state_of(run)
    return {'template_id':run['template_id'],'template_version':run['template_version'],'label':run['label'],
            'state':run['state'],'active_step':run['active_step'],'active_label':plan.step(run['active_step']).label,
            'completed_count':len(completed),'total_steps':len(plan.steps)}


def guard_transfer(con,ticket_id):
    if active_run(con,ticket_id):
        raise QueueError('Пациент проходит маршрут. Завершите этап через управление маршрутом.',409)


def finish_or_cancel(con,ticket_id,action,ts):
    run=active_run(con,ticket_id)
    if not run:return
    plan,completed=state_of(run)
    if action=='finish':
        if len(completed)+1!=len(plan.steps):
            raise QueueError('Есть оставшиеся этапы. Выберите следующий разрешённый кабинет.',409)
        completed.append(run['active_step'])
        con.execute("UPDATE visit_routes SET completed=?,state='complete',ended_at=? WHERE ticket_id=?",
                    (json.dumps(completed),ts,ticket_id))
    elif action=='cancel':
        con.execute("UPDATE visit_routes SET state='cancelled',ended_at=? WHERE ticket_id=?",(ts,ticket_id))
