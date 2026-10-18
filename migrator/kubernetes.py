"""Read-only kubectl adapter. Never changes the current context or credentials."""
import json
import hashlib
import re
import shutil
import subprocess
import secrets
import time
import threading
from contextvars import ContextVar

progress_observer = ContextVar("progress_observer", default=None)

def progress(message, phase="step"):
    observer = progress_observer.get()
    if observer:
        observer(message, phase)

from .core import normalize, reason, safe_manifest
from .core import KINDS, PROTECTED, analyze
from . import history
from .readiness import evaluate
from .dependencies import references


class ClusterError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def run(args, payload=None):
    executable = shutil.which('kubectl')
    if not executable:
        raise ClusterError('kubectl is not installed on the server.', 503)
    label = 'kubectl request'
    for verb in ('get-contexts', 'api-resources', 'get'):
        if verb in args:
            i = args.index(verb)
            label = verb + (' ' + args[i+1] if verb == 'get' and i+1 < len(args) else '')
            break
    context = args[args.index('--context')+1] if '--context' in args else 'local kubeconfig'
    ns = args[args.index('--namespace')+1] if '--namespace' in args else None
    label = context + (' / ' + ns if ns else '') + ' · ' + label
    progress('Waiting: ' + label, 'waiting')
    timeout = 25
    if 'get' in args:
        resource_index = args.index('get') + 1
        if resource_index < len(args):
            timeout = 25 * len(args[resource_index].split(','))
    try:
        result = subprocess.run([executable, '--request-timeout=15s', *args],
                                capture_output=True, text=True, timeout=timeout,
                                **({'input': json.dumps(payload)} if payload is not None else {'stdin': subprocess.DEVNULL}))
    except subprocess.TimeoutExpired:
        raise ClusterError('Cluster request timed out. Check connectivity and authentication.') from None
    except OSError:
        raise ClusterError('Unable to start kubectl.', 503) from None
    if result.returncode:
        # Never return raw plugin stderr: it can contain credentials.
        error = result.stderr.lower()
        if any(text in error for text in ('timeout', 'timed out', 'deadline exceeded')):
            message = 'Kubernetes request timed out while waiting for an API or authentication response.'
        elif 'forbidden' in error:
            message = 'Kubernetes denied access (RBAC Forbidden).'
        elif 'unauthorized' in error or 'logged in' in error:
            message = 'Kubernetes authentication failed. Refresh server-side credentials.'
        elif 'executable' in error or 'exec plugin' in error:
            message = 'Kubeconfig authentication plugin is missing or failed.'
        elif 'conflict' in error:
            message = 'Target field ownership or resource version conflict. Refresh the comparison; no forced overwrite was performed.'
        elif 'immutable' in error or 'invalid' in error:
            message = 'Kubernetes rejected the manifest (validation or immutable field). Check resource compatibility.'
        elif 'notfound' in error or 'not found' in error:
            message = 'Resource or target namespace not found. Create the target namespace before syncing.'
        else:
            message = 'kubectl failed. Check server-side kubeconfig, credentials and network access.'
        raise ClusterError(message)
    progress('Completed: ' + label, 'completed')
    return result.stdout


def contexts():
    return {'contexts': [name for name in run(['config', 'get-contexts', '-o', 'name']).splitlines() if name]}


def context_args(context):
    if not isinstance(context, str) or context not in contexts()['contexts']:
        raise ClusterError('Selected context is not present in the server kubeconfig. Click Load cluster contexts and select an exact name.', 400)
    return ['--context', context]


def read_json(args):
    try:
        value = json.loads(run(args))
    except json.JSONDecodeError:
        raise ClusterError('kubectl returned invalid JSON.') from None
    if not isinstance(value, dict):
        raise ClusterError('kubectl returned an invalid resource response.')
    return value


def namespaces(context):
    args = context_args(context)
    result = read_json([*args, 'get', 'namespaces', '-o', 'json'])
    return {'namespaces': sorted(obj['metadata']['name'] for obj in result['items']
                                 if obj['metadata']['name'] not in PROTECTED)}


def validate_selection(source, target, selected):
    if source == target:
        raise ValueError('Source and target contexts must be distinct')
    if not isinstance(selected, list) or not 1 <= len(selected) <= 50 or len(set(selected)) != len(selected):
        raise ValueError('Select between 1 and 50 unique namespaces')
    if any(not isinstance(ns, str) or len(ns) > 63 or not re.fullmatch(r'[a-z0-9](?:[-a-z0-9]*[a-z0-9])?', ns) or ns in PROTECTED for ns in selected):
        raise ValueError('Invalid or protected namespace')


def snapshot(context, selected):
    args = context_args(context)
    ns_response = read_json([*args, 'get', 'namespaces', '-o', 'json'])
    available = {obj['metadata']['name']: obj for obj in ns_response['items']}
    # Discovery handles optional CRDs without swallowing authorization errors.
    discovery = run([*args, 'api-resources', '--namespaced=true', '--verbs=list', '--no-headers', '-o', 'wide'])
    supported = set()
    for line in discovery.splitlines():
        columns = line.split()
        if 'true' not in columns or columns.index('true') + 1 >= len(columns):
            raise ClusterError('Invalid API discovery response.')
        supported.add(columns[columns.index('true') + 1])
    kinds = sorted((KINDS - {'Namespace'}) & supported)
    missing = sorted((KINDS - {'Namespace'}) - supported)
    resources = []
    for ns in selected:
        if ns not in available:
            progress(context + ' / ' + ns + ' · namespace absent; skipping')
            continue
        resources.append(available[ns])
        if kinds:
            # One process per namespace avoids repeated kubectl startup, discovery
            # and exec-auth plugin startup for every kind. API lists remain scoped.
            result = read_json([*args, '--namespace', ns, 'get', ','.join(kinds), '-o', 'json'])
            if not isinstance(result.get('items'), list):
                raise ClusterError('kubectl returned an invalid resource list.')
            resources.extend(result['items'])
    return resources, missing


def live_analyze(body):
    source, target, selected = body['source_context'], body['target_context'], body['namespaces']
    validate_selection(source, target, selected)
    snapshots = []
    for side, context in [('Source', source), ('Target', target)]:
        progress(side + ' cluster: ' + context + ' · reading snapshot')
        try:
            snapshots.append(snapshot(context.strip(), selected))
        except ClusterError as error:
            raise ClusterError(f'{side} cluster: {error}', error.status) from None
        except (KeyError, TypeError, AttributeError, ValueError):
            raise ClusterError(f'{side} cluster returned an unexpected resource structure.') from None
    (src, src_missing), (dst, dst_missing) = snapshots
    progress('Comparing and normalizing source and target resources', 'comparing')
    report = analyze(src, dst, selected, body.get('old_registry', ''), body.get('new_registry', ''))
    report.update(source_context=source, target_context=target,
                  unavailable_kinds={'source': src_missing, 'target': dst_missing},
                  notice='Read-only cluster snapshot. Sequential reads are not atomic. No cluster writes. Secret values are redacted; persistent data is outside scope.')
    return report


_PLANS = {}
_LOCK = threading.Lock()


def sync_plan(body):
    source, target = body['source_context'], body['target_context']
    ns, name, kind = body['namespace'], body['name'], body['kind']
    validate_selection(source, target, [ns])
    if kind not in KINDS - {'Namespace'} or not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9.-]*', name):
        raise ValueError('Select a supported namespaced resource')
    api = body['apiVersion']
    if not isinstance(api, str) or not re.fullmatch(r'[a-z0-9./-]+', api):
        raise ValueError('Invalid API version')
    group = api.split('/')[0] if '/' in api else ''
    resource = kind + ('.' + group if group else '')
    src_args, dst_args = context_args(source), context_args(target)
    obj = read_json([*src_args, '-n', ns, 'get', resource, name, '-o', 'json'])
    if reason(obj) or obj['metadata'].get('namespace') != ns:
        raise ValueError('Resource is excluded by migration policy')
    target_obj = None
    raw = run([*dst_args, '-n', ns, 'get', resource, name, '--ignore-not-found', '-o', 'json'])
    if raw.strip():
        try:
            target_obj = json.loads(raw)
        except json.JSONDecodeError:
            raise ClusterError('Target returned invalid JSON.') from None
    if target_obj and reason(target_obj):
        raise ValueError('Target resource is controller-managed or excluded; sync is blocked')
    mode = body.get('mode', 'prepare')
    if mode not in ('prepare', 'activate'):
        raise ValueError('Select prepare or activate mode')
    if mode == 'prepare' and kind in ('DaemonSet', 'ActiveMQArtemis', 'HorizontalPodAutoscaler'):
        raise ValueError('Passive preparation is unsupported for this kind. Review dependencies and explicitly select activation.')
    manifest = normalize(obj, body.get('old_registry', ''), body.get('new_registry', ''))
    if mode == 'prepare' and kind in ('Deployment', 'StatefulSet'):
        hpas = read_json([*dst_args, '-n', ns, 'get', 'horizontalpodautoscalers', '-o', 'json'])
        if any(hpa.get('spec', {}).get('scaleTargetRef', {}).get('name') == name and
               hpa.get('spec', {}).get('scaleTargetRef', {}).get('kind') == kind for hpa in hpas.get('items', [])):
            raise ValueError('Target HPA controls this workload and can reactivate it. Resolve the HPA before passive preparation.')
        manifest.setdefault('spec', {})['replicas'] = 0
    if mode == 'prepare' and kind == 'CronJob':
        manifest.setdefault('spec', {})['suspend'] = True
    # Verify referenced objects exist without retrieving Secret payloads.
    for dependency_kind, dependency_name in references(manifest):
        if not run([*dst_args, '-n', ns, 'get', dependency_kind, dependency_name,
                    '--ignore-not-found', '-o', 'name']).strip():
            raise ValueError('Target dependency missing: ' + dependency_kind + '/' + dependency_name)
    if kind == 'StatefulSet':
        for claim in manifest.get('spec', {}).get('volumeClaimTemplates', []) or []:
            storage_class = claim.get('spec', {}).get('storageClassName')
            if storage_class and not run([*dst_args, 'get', 'storageclass', storage_class,
                                          '--ignore-not-found', '-o', 'name']).strip():
                raise ValueError('Target StorageClass missing: ' + storage_class)
    version = target_obj['metadata']['resourceVersion'] if target_obj else None
    if version:
        manifest['metadata']['resourceVersion'] = version
    action = ['apply', '--server-side', '--field-manager=aks-migrator'] if target_obj else ['create']
    # Admission, field conflicts, immutable fields and missing namespace fail here.
    run([*dst_args, '-n', ns, *action, '--dry-run=server', '-f', '-', '-o', 'json'], manifest)
    token = secrets.token_urlsafe(32)
    plan = dict(target=target, namespace=ns, resource=resource, name=name,
                version=version, manifest=manifest, action=action, before=target_obj, source=source, mode=mode, kind=kind, expires=time.monotonic()+300)
    with _LOCK:
        for key in list(_PLANS):
            if _PLANS[key]['expires'] < time.monotonic():
                del _PLANS[key]
        if len(_PLANS) >= 100:
            raise ClusterError('Too many pending sync previews. Try again later.', 429)
        _PLANS[token] = plan
    return {'token': token, 'manifest': safe_manifest(manifest), 'target_context': target,
            'operation': 'update' if target_obj else 'create', 'mode': mode,
            'warnings': ['Preparation can stop an existing target workload. Activation may duplicate source processing. CronJob suspend does not stop already running jobs.',
                         'Reference existence is checked; image pull, Workload Identity, network access, data integrity and application health require separate validation.',
                         'Secret rollback backups are not stored.' if kind == 'Secret' else 'A local rollback snapshot will be saved.'],
            'expires_in': 300}


def fingerprint(obj):
    return hashlib.sha256(json.dumps(normalize(obj, transform=False), sort_keys=True).encode()).hexdigest()


def sync_execute(body):
    if body.get('confirm') is not True:
        raise ValueError('Explicit confirmation is required')
    with _LOCK:
        plan = _PLANS.pop(body.get('token', ''), None)
    if not plan or plan.get('rollback') or plan['expires'] < time.monotonic():
        raise ValueError('Sync preview expired or already used. Preview again.')
    args = context_args(plan['target'])
    raw = run([*args, '-n', plan['namespace'], 'get', plan['resource'], plan['name'], '--ignore-not-found', '-o', 'json'])
    current = json.loads(raw) if raw.strip() else None
    version = current['metadata']['resourceVersion'] if current else None
    if version != plan['version']:
        raise ClusterError('Target changed after preview. Analyze and preview again.', 409)
    # Journal must be durable before attempting the write. Secret content is excluded.
    record = history.save({'source': plan['source'], 'target': plan['target'], 'namespace': plan['namespace'],
                           'resource': plan['resource'], 'name': plan['name'], 'kind': plan['kind'],
                           'mode': plan['mode'], 'before': plan['before'] if plan['kind'] != 'Secret' else None,
                           'rollback_available': False, 'status': 'Applying'})
    try:
        raw = run([*args, '-n', plan['namespace'], *plan['action'], '-f', '-', '-o', 'json'], plan['manifest'])
        applied = json.loads(raw)
        meta = applied['metadata']
        record.update(status='Applied', applied_uid=meta['uid'], applied_version=meta['resourceVersion'],
                      rollback_available=plan['kind'] != 'Secret', applied_fingerprint=fingerprint(applied))
        history.save(record)
    except Exception:
        # A timeout can happen after an API write succeeded; never claim no change.
        record.update(status='Unknown', rollback_available=False)
        try:
            history.save(record)
        except Exception:
            pass  # Initial Applying record remains; the write result is uncertain.
        raise ClusterError('Write outcome is uncertain. Inspect target and operation history before retrying.') from None
    return {'status': 'Applied', 'operation_id': record['id'], 'mode': plan['mode'],
            'target_context': plan['target'], 'namespace': plan['namespace'], 'name': plan['name']}


def operation_health(body):
    record = history.get(body['operation_id'])
    args = context_args(record['target'])
    command = [*args, '-n', record['namespace'], 'get', record['resource'], record['name'], '-o', 'json']
    if record.get('status') == 'DeletionRequested':
        raw = run([*command, '--ignore-not-found'])
        obj = json.loads(raw) if raw.strip() else None
        if obj is None:
            health = {'state': 'Deleted', 'detail': 'The created target resource is absent'}
        elif obj['metadata'].get('uid') != record.get('applied_uid'):
            raise ClusterError('A different resource now exists with this name.', 409)
        else:
            health = {'state': 'Terminating', 'detail': 'Waiting for target deletion/finalizers'}
    else:
        obj = read_json(command)
        if obj['metadata'].get('uid') != record.get('applied_uid'):
            raise ClusterError('Resource identity changed after this operation.', 409)
        if record.get('status') == 'Applied' and fingerprint(obj) != record.get('applied_fingerprint'):
            health = {'state': 'Drifted', 'detail': 'Target configuration changed after this operation; review the comparison'}
        else:
            health = evaluate(obj, record['mode'])
    # Do not update the rollback version: changes by controllers/users must not be overwritten.
    history.update(record['id'], {'health': health})
    return health


def rollback_plan(body):
    record = history.get(body['operation_id'])
    if not record.get('rollback_available'):
        raise ValueError('Rollback is unavailable for this operation (Secret, uncertain write or already rolled back)')
    args = context_args(record['target'])
    current = read_json([*args, '-n', record['namespace'], 'get', record['resource'], record['name'], '-o', 'json'])
    if current['metadata'].get('uid') != record['applied_uid']:
        raise ClusterError('Target was replaced after sync. Rollback blocked.', 409)
    if reason(current):
        raise ClusterError('Target is now controller-managed or excluded. Automatic rollback blocked.', 409)
    if fingerprint(current) != record.get('applied_fingerprint'):
        raise ClusterError('Target configuration changed after sync. Automatic rollback blocked; review and recover manually.', 409)
    # Status updates can advance resourceVersion; compare desired content instead of
    # rejecting controller-only status changes. The reviewed version is still used
    # as an atomic precondition for rollback execution.
    before = record['before']
    manifest = None
    if before:
        manifest = json.loads(json.dumps(before))
        for key in ('managedFields', 'creationTimestamp', 'generation'):
            manifest.get('metadata', {}).pop(key, None)
        manifest.pop('status', None)
        manifest['metadata']['resourceVersion'] = current['metadata']['resourceVersion']
        run([*args, '-n', record['namespace'], 'replace', '--dry-run=server', '-f', '-', '-o', 'json'], manifest)
    token = secrets.token_urlsafe(32)
    with _LOCK:
        for key in list(_PLANS):
            if _PLANS[key]['expires'] < time.monotonic():
                del _PLANS[key]
        if len(_PLANS) >= 100:
            raise ClusterError('Too many pending previews. Try again later.', 429)
        _PLANS[token] = {'rollback': record, 'manifest': manifest, 'version': current['metadata']['resourceVersion'],
                         'uid': current['metadata']['uid'], 'expires': time.monotonic()+300}
    return {'token': token, 'operation': 'restore' if manifest else 'delete-created-resource',
            'target_context': record['target'], 'manifest': safe_manifest(manifest) if manifest else None,
            'namespace': record['namespace'], 'name': record['name'], 'kind': record['kind'],
            'warning': 'Rollback restores the saved configuration or deletes the resource created by this sync. Review current state first.'}


def rollback_execute(body):
    if body.get('confirm') is not True:
        raise ValueError('Explicit rollback confirmation required')
    with _LOCK:
        plan = _PLANS.pop(body.get('token', ''), None)
    if not plan or not plan.get('rollback') or plan['expires'] < time.monotonic():
        raise ValueError('Rollback preview expired or already used')
    record = plan['rollback']
    args = context_args(record['target'])
    current = read_json([*args, '-n', record['namespace'], 'get', record['resource'], record['name'], '-o', 'json'])
    meta = current['metadata']
    if meta.get('uid') != plan['uid'] or meta.get('resourceVersion') != plan['version']:
        raise ClusterError('Target changed after rollback preview. Preview again.', 409)
    # Save intent before the write; retain the snapshot for manual recovery on failure.
    history.update(record['id'], {'status': 'RollingBack', 'rollback_available': False}, require_rollback=True)
    try:
        if plan['manifest']:
            run([*args, '-n', record['namespace'], 'replace', '-f', '-', '-o', 'json'], plan['manifest'])
        else:
            api = current['apiVersion']
            prefix = '/api/' + api if '/' not in api else '/apis/' + api
            # Resolve the plural using API discovery rather than guessing from Kind.
            discovery = read_json([*args, 'get', '--raw', prefix])
            plural = next(r['name'] for r in discovery['resources'] if r['kind'] == current['kind'] and r.get('namespaced') and '/' not in r['name'])
            from urllib.parse import quote
            path = prefix + '/namespaces/' + quote(record['namespace'], safe='') + '/' + plural + '/' + quote(record['name'], safe='')
            options = {'apiVersion': 'v1', 'kind': 'DeleteOptions',
                       'preconditions': {'uid': plan['uid'], 'resourceVersion': plan['version']}}
            run([*args, 'delete', '--raw', path, '-f', '-'], options)
    except Exception:
        history.update(record['id'], {'status': 'RollbackUnknown', 'rollback_available': False})
        raise ClusterError('Rollback outcome is uncertain. Inspect the target and saved history.') from None
    state = 'RollbackApplied' if plan['manifest'] else 'DeletionRequested'
    history.update(record['id'], {'status': state, 'rollback_available': False})
    return {'status': state, 'operation_id': record['id']}
