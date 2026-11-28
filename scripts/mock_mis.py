"""CLI for a durable synthetic provider; no actual MIS connection."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from app.integrations.contracts import AppointmentEvent,AppointmentSnapshot
from app.integrations.mock import MockMISAdapter
from app.integrations.worker import Worker
from app.repositories.postgres import database_url_from_env
from app.queue import Queue,PostgresQueue


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mock-db',default=os.environ.get('CLINIC_MOCK_MIS_PATH','data/mock-mis.sqlite3'))
    sub=parser.add_subparsers(dest='command',required=True)
    publish=sub.add_parser('publish',help='Append a synthetic full appointment snapshot')
    publish.add_argument('--external-id',required=True)
    publish.add_argument('--version',required=True,type=int)
    publish.add_argument('--booking-code',required=True)
    publish.add_argument('--room',required=True)
    publish.add_argument('--scheduled-at',required=True,help='ISO 8601 with offset')
    publish.add_argument('--state',choices=['booked','cancelled'],default='booked')
    publish.add_argument('--event-id',default=None,help='Stable event ID for replay testing')
    work=sub.add_parser('worker')
    work.add_argument('--once',action='store_true')
    work.add_argument('--interval',type=float,default=2)
    sub.add_parser('received')
    args=parser.parse_args()
    path=Path(os.environ.get('CLINIC_DB_PATH','data/clinic.sqlite3')).resolve()
    if Path(args.mock_db).resolve()==path:
        parser.error('Mock provider must use a separate SQLite file')
    adapter=MockMISAdapter(args.mock_db)
    if args.command=='publish':
        event=AppointmentEvent(event_id=args.event_id or str(uuid4()),appointment=AppointmentSnapshot(
            external_id=args.external_id,version=args.version,booking_code=args.booking_code,room_id=args.room,
            scheduled_at=datetime.fromisoformat(args.scheduled_at),state=args.state))
        print(json.dumps({'event_id':event.event_id,'position':adapter.publish(event)}))
    elif args.command=='received':
        print(json.dumps(adapter.received(),ensure_ascii=False,indent=2))
    else:
        if not 0.1<=args.interval<=300:
            parser.error('Interval must be 0.1–300 seconds')
        url=database_url_from_env()
        queue=PostgresQueue(url) if url else Queue(path)
        try:
            queue.initialize()
            worker=Worker(queue,adapter)
            while True:
                result=worker.cycle()
                if args.once or result['pulled'] or result['delivered'] or result['failed'] or result['pull_error']:
                    print(json.dumps(result),flush=True)
                if args.once:
                    return 1 if result['pull_error'] or result['failed'] else 0
                time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0
        finally:
            queue.close()
    return 0


if __name__=='__main__':
    raise SystemExit(main())
