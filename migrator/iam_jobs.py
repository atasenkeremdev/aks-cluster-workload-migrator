"""Independent bounded IAM jobs; progress never contains identity configuration."""
import copy
from . import auth
import secrets
import threading
import time
from .kubernetes import ClusterError, progress_observer
from .iam import analyze, validate_request

_JOBS = {}
_LOCK = threading.Lock()
TTL = 600


def start(body):
    validate_request(body)
    now = time.monotonic()
    with _LOCK:
        for key in list(_JOBS):
            if _JOBS[key]['state'] != 'running' and now - _JOBS[key]['updated'] > TTL:
                del _JOBS[key]
        if sum(j['state'] == 'running' for j in _JOBS.values()) >= 4 or len(_JOBS) >= 30:
            raise ClusterError('IAM inventory queue is full. Wait for running scans to finish.', 429)
        job_id = secrets.token_urlsafe(24)
        _JOBS[job_id] = {'state': 'running', 'started': now, 'updated': now,
                         'step_started': now, 'current': 'Starting IAM inventory', 'events': [],
                         'completed_commands': 0, 'owner':auth.owner()}
    threading.Thread(target=auth.run_as, args=(auth.current_session.get(), _worker, job_id, copy.deepcopy(body)), daemon=True).start()
    return {'job_id': job_id}


def _worker(job_id, body):
    def notify(message, phase='step'):
        now = time.monotonic()
        with _LOCK:
            job = _JOBS[job_id]
            job.update(current=message, updated=now, step_started=now, phase=phase)
            if phase == 'completed':
                job['completed_commands'] += 1
            job['events'].append({'elapsed': round(now-job['started'], 1), 'message': message})
            job['events'] = job['events'][-100:]
    token = progress_observer.set(notify)
    try:
        result = analyze(body)
        notify('IAM inventory complete', 'completed_analysis')
        with _LOCK:
            _JOBS[job_id].update(state='completed', result=result)
    except Exception as error:
        message = str(error) if isinstance(error, ClusterError) else 'IAM inventory failed while reading identity and access configuration.'
        notify(message, 'failed')
        with _LOCK:
            _JOBS[job_id].update(state='failed', error=message)
    finally:
        progress_observer.reset(token)


def status(job_id):
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            raise ClusterError('IAM scan no longer exists. Start a new IAM inventory.', 404)
        auth.check_owner(job)
        result = copy.deepcopy(job)
    now = time.monotonic()
    end = now if result['state'] == 'running' else result['updated']
    result['elapsed_seconds'] = round(end-result.pop('started'), 1)
    result['step_seconds'] = round(end-result.pop('step_started'), 1)
    result.pop('updated')
    return result
