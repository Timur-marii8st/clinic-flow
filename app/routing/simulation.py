"""Seeded discrete-event comparison on synthetic, identical demand.

No patient data, staff roster, appointments, urgency, breaks or travel time.
Every patient follows the same supplied approved DAG. Not a clinical forecast.
"""
from dataclasses import dataclass
import random
import simpy
from .plan import Plan
from .eta import percentile


@dataclass(frozen=True)
class Patient:
    arrival: float
    durations: dict


def workload(plan:Plan,count=100,interarrival=120,mean_service=300,seed=42):
    if not 1<=count<=5000 or not 1<=interarrival<=3600 or not 1<=mean_service<=7200:
        raise ValueError('Synthetic scenario outside supported bounds')
    rng=random.Random(seed)
    arrival=0.0
    patients=[]
    for _ in range(count):
        arrival+=rng.expovariate(1/interarrival)
        durations={s.id:rng.uniform(mean_service*.5,mean_service*1.5) for s in plan.steps}
        patients.append(Patient(arrival,durations))
    return patients


def simulate(plan:Plan,patients,policy='fixed',typical_seconds=300):
    if policy not in ('fixed','workload'):
        raise ValueError('Unknown simulation policy')
    if not patients or typical_seconds<=0:
        raise ValueError('Empty scenario or invalid expected duration')
    env=simpy.Environment()
    resources={room:simpy.Resource(env,capacity=1) for step in plan.steps for room in step.rooms}
    expected_finish={room:0.0 for room in resources}
    waits,visits=[],[]
    by_room={room:{'stages':0,'service_seconds':0.0} for room in resources}
    def patient(p):
        yield env.timeout(max(0,p.arrival-env.now))
        done=[]
        while len(done)<len(plan.steps):
            options=[(s,r) for s in plan.ready(done) for r in s.rooms]
            if policy=='workload':
                step,room=min(options,key=lambda pair:(
                    max(0,expected_finish[pair[1]]-env.now)+len(resources[pair[1]].queue)*typical_seconds,
                    pair[0].id,pair[1]))
            else:
                step,room=options[0]
            entered=env.now
            with resources[room].request() as request:
                yield request
                waits.append(env.now-entered)
                # Policy sees expected service time, not the sampled future duration.
                expected_finish[room]=env.now+typical_seconds
                duration=p.durations[step.id]
                yield env.timeout(duration)
                by_room[room]['stages']+=1
                by_room[room]['service_seconds']+=duration
                expected_finish[room]=env.now
            done.append(step.id)
        visits.append(env.now-p.arrival)
    for p in patients:env.process(patient(p))
    env.run()
    return {'policy':policy,'patients':len(visits),'stage_samples':len(waits),
            'wait_p50_seconds':percentile(waits,.5),'wait_p90_seconds':percentile(waits,.9),
            'visit_p50_seconds':percentile(visits,.5),'visit_p90_seconds':percentile(visits,.9),
            'makespan_seconds':round(env.now,1),'rooms':by_room}


def compare(plan,count=100,interarrival=120,mean_service=300,seed=42):
    patients=workload(plan,count,interarrival,mean_service,seed)
    return {'synthetic':True,'seed':seed,'count':count,'mean_interarrival_seconds':interarrival,
            'mean_service_seconds':mean_service,
            'results':[simulate(plan,patients,policy,mean_service) for policy in ('fixed','workload')]}
