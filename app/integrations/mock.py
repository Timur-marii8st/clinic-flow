"""Persistent synthetic provider in a SEPARATE SQLite file.

Independent transactions simulate a real remote system's commit boundary.
No real patient data and no vendor API calls. Delivery deduplicates by ID.
"""
from contextlib import contextmanager, closing
from pathlib import Path
import sqlite3

from .contracts import AppointmentEvent, FeedItem, canonical_json


class MockMISAdapter:
    source='mock'

    def __init__(self,path):
        self.path=Path(path).resolve()
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connection() as con:
            con.execute('''CREATE TABLE IF NOT EXISTS feed (
                position INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT NOT NULL UNIQUE,payload TEXT NOT NULL)''')
            con.execute('''CREATE TABLE IF NOT EXISTS received (
                event_id TEXT PRIMARY KEY,payload TEXT NOT NULL)''')

    @contextmanager
    def connection(self):
        with closing(sqlite3.connect(self.path,timeout=10,isolation_level=None)) as con:
            con.row_factory=sqlite3.Row
            con.execute('BEGIN IMMEDIATE')
            try:
                yield con
                con.commit()
            except BaseException:
                con.rollback()
                raise

    def publish(self,event: AppointmentEvent):
        payload=canonical_json(event.canonical())
        with self.connection() as con:
            row=con.execute('SELECT position,payload FROM feed WHERE event_id=?',(event.event_id,)).fetchone()
            if row:
                if row['payload']!=payload:
                    raise ValueError('Mock event ID collision')
                return row['position']
            return con.execute('INSERT INTO feed(event_id,payload) VALUES (?,?)',
                               (event.event_id,payload)).lastrowid

    def pull(self,after,limit):
        with self.connection() as con:
            return [FeedItem(position=r['position'],event=AppointmentEvent.model_validate_json(r['payload']))
                    for r in con.execute('SELECT * FROM feed WHERE position>? ORDER BY position LIMIT ?',(after,limit))]

    def deliver(self,event):
        payload=canonical_json(event)
        with self.connection() as con:
            row=con.execute('SELECT payload FROM received WHERE event_id=?',(event['event_id'],)).fetchone()
            if row:
                if row['payload']!=payload:
                    raise ValueError('Mock delivery ID collision')
                return
            con.execute('INSERT INTO received VALUES (?,?)',(event['event_id'],payload))

    def received(self):
        import json
        with self.connection() as con:
            return [json.loads(r['payload']) for r in con.execute('SELECT payload FROM received ORDER BY rowid')]
