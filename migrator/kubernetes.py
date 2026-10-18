"""Read-only kubectl adapter. Never changes the current context or credentials."""
import json
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
    manifest = normalize(obj, body.get('old_registry', ''), body.get('new_registry', ''))
    version = target_obj['metadata']['resourceVersion'] if target_obj else None
    if version:
        manifest['metadata']['resourceVersion'] = version
    action = ['apply', '--server-side', '--field-manager=aks-migrator'] if target_obj else ['create']
    # Admission, field conflicts, immutable fields and missing namespace fail here.
    run([*dst_args, '-n', ns, *action, '--dry-run=server', '-f', '-', '-o', 'json'], manifest)
    token = secrets.token_urlsafe(32)
    plan = dict(target=target, namespace=ns, resource=resource, name=name,
                version=version, manifest=manifest, action=action, expires=time.monotonic()+300)
    with _LOCK:
        for key in list(_PLANS):
            if _PLANS[key]['expires'] < time.monotonic():
                del _PLANS[key]
        if len(_PLANS) >= 100:
            raise ClusterError('Too many pending sync previews. Try again later.', 429)
        _PLANS[token] = plan
    return {'token': token, 'manifest': safe_manifest(manifest), 'target_context': target,
            'operation': 'update' if target_obj else 'create', 'expires_in': 300}


def sync_execute(body):
    if body.get('confirm') is not True:
        raise ValueError('Explicit confirmation is required')
    with _LOCK:
        plan = _PLANS.pop(body.get('token', ''), None)
    if not plan or plan['expires'] < time.monotonic():
        raise ValueError('Sync preview expired or already used. Preview again.')
    args = context_args(plan['target'])
    raw = run([*args, '-n', plan['namespace'], 'get', plan['resource'], plan['name'], '--ignore-not-found', '-o', 'json'])
    current = json.loads(raw) if raw.strip() else None
    version = current['metadata']['resourceVersion'] if current else None
    if version != plan['version']:
        raise ClusterError('Target changed after preview. Analyze and preview again.', 409)
    run([*args, '-n', plan['namespace'], *plan['action'], '-f', '-', '-o', 'json'], plan['manifest'])
    return {'status': 'synced', 'target_context': plan['target'], 'namespace': plan['namespace'], 'name': plan['name']}
