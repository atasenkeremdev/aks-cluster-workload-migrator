"""Local durable operation journal. Raw Secret manifests are never persisted."""
from . import auth
from contextlib import contextmanager
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


def path():
    return Path(os.environ.get('MIGRATOR_STATE_DIR', str(Path(__file__).resolve().parent.parent / '.state'))) / 'history.sqlite3'


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
    record=auth.bind_owner(record)
    record = {**record, 'created_at': record.get('created_at', datetime.now(timezone.utc).isoformat()),
              'id': record.get('id', uuid.uuid4().hex),
              'updated_at': datetime.now(timezone.utc).isoformat()}
    with connect() as db:
        db.execute('INSERT OR REPLACE INTO operations VALUES (?, ?)', (record['id'], json.dumps(record)))
    return record


def get(operation_id):
    with connect() as db:
        row = db.execute('SELECT data FROM operations WHERE id=?', (operation_id,)).fetchone()
    if not row:
        raise ValueError('Operation not found')
    record=json.loads(row[0]);auth.check_owner(record)
    return record


def listing():
    with connect() as db:
        if auth.enabled():rows=db.execute("SELECT data FROM operations WHERE json_extract(data, '$.owner')=? ORDER BY rowid DESC LIMIT 100",(auth.owner(),)).fetchall()
        else:rows = db.execute('SELECT data FROM operations ORDER BY rowid DESC LIMIT 100').fetchall()
    # Backups may contain ConfigMap configuration; never expose raw snapshots.
    return {'operations': [{key:value for key,value in json.loads(row[0]).items()
                            if key not in ('before',)} for row in rows
                           if not auth.enabled() or json.loads(row[0]).get('owner')==auth.owner()]}


def update(operation_id, changes, *, require_rollback=False, expected_status=None):
    """Merge atomically so readiness polling cannot resurrect a rollback action."""
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT data FROM operations WHERE id=?', (operation_id,)).fetchone()
        if not row:
            raise ValueError('Operation not found')
        record = json.loads(row[0]);auth.check_owner(record)
        if expected_status is not None and record.get('status') != expected_status:
            raise ValueError('Operation changed during readiness check')
        if require_rollback and not record.get('rollback_available'):
            raise ValueError('Rollback already started or unavailable')
        record.update(changes)
        record['updated_at'] = datetime.now(timezone.utc).isoformat()
        db.execute('UPDATE operations SET data=? WHERE id=?', (json.dumps(record), operation_id))
    return record
