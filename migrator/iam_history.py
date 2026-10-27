"""Separate durable Azure IAM operation journal; never mixed with workload history."""
from contextlib import contextmanager
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


def path():
    return Path(os.environ.get('MIGRATOR_STATE_DIR', str(Path(__file__).resolve().parent.parent / '.state'))) / 'iam-history.sqlite3'


@contextmanager
def connect():
    location = path()
    location.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Create the database with restricted POSIX permissions before sqlite opens it.
    descriptor = os.open(location, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(descriptor)
    db = sqlite3.connect(location, timeout=10)
    db.execute('CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
    try:
        with db:
            yield db
    finally:
        db.close()


def save(record):
    record = {**record, 'created_at': record.get('created_at', datetime.now(timezone.utc).isoformat()),
              'id': record.get('id', uuid.uuid4().hex),
              'updated_at': datetime.now(timezone.utc).isoformat()}
    with connect() as db:
        db.execute('INSERT OR REPLACE INTO operations VALUES (?, ?)', (record['id'], json.dumps(record)))
    return record


def listing():
    with connect() as db:
        rows = db.execute('SELECT data FROM operations ORDER BY rowid DESC LIMIT 100').fetchall()
    # Backups may contain ConfigMap configuration; never expose raw snapshots.
    return {'operations': [{key:value for key,value in json.loads(row[0]).items()
                            if key not in ('before',)} for row in rows]}

