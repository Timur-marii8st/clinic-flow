"""Exercise data migration against real PostgreSQL in isolated test schemas."""
import sqlite3
from contextlib import closing

import pytest

from app.queue import Queue, PostgresQueue
from scripts.import_sqlite import import_demo


@pytest.mark.parametrize('version', [2,3])
def test_import_preserves_ids_and_legs(tmp_path, postgres_url, version):
    path = tmp_path / 'old.sqlite3'
    source = Queue(path)
    source.initialize()
    ticket = source.walk_in('101', 'walk-key-import-demo')
    source.call_next('101', 'call-key-import-demo')
    source.act('101',ticket['id'],'start')
    source.transfer('101',ticket['id'],'102')
    expected = source.history(ticket['id'])
    if version == 2:
        with closing(sqlite3.connect(path)) as con:
            con.execute('DROP TABLE command_keys')
            con.execute('PRAGMA user_version=2')
            con.commit()
    with closing(sqlite3.connect(path)) as con:
        original_version = con.execute('PRAGMA user_version').fetchone()[0]
    counts = import_demo(path, postgres_url)
    assert counts['rooms'] == 3 and counts['tickets'] == 4 and counts['queue_legs'] == 2
    assert counts['command_keys'] == (1 if version == 3 else 0)
    target = PostgresQueue(postgres_url)
    try:
        target.initialize()
        assert target.history(ticket['id']) == expected
        if version == 3:
            assert target.call_next('101','call-key-import-demo')['id'] == ticket['id']
        assert target.walk_in('101')['id'] > ticket['id']
        with pytest.raises(ValueError,match='empty'):
            import_demo(path,postgres_url)
    finally:
        target.close()
    with closing(sqlite3.connect(path)) as con:
        assert con.execute('PRAGMA user_version').fetchone()[0] == original_version
        assert con.execute('SELECT COUNT(*) FROM tickets').fetchone()[0] == 4
