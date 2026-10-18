"""Read-only kubectl adapter. Never changes the current context or credentials."""
import json
import re
import shutil
import subprocess
from .core import KINDS, PROTECTED, analyze


class ClusterError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def run(args):
    executable = shutil.which('kubectl')
    if not executable:
        raise ClusterError('kubectl is not installed on the server.', 503)
    try:
        result = subprocess.run([executable, '--request-timeout=15s', *args],
                                capture_output=True, text=True, timeout=25, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise ClusterError('Cluster request timed out. Check connectivity and authentication.') from None
    except OSError:
        raise ClusterError('Unable to start kubectl.', 503) from None
    if result.returncode:
        # Never return raw plugin stderr: it can contain credentials.
        error = result.stderr.lower()
        if 'forbidden' in error:
            message = 'Kubernetes denied access (RBAC Forbidden).'
        elif 'unauthorized' in error or 'logged in' in error:
            message = 'Kubernetes authentication failed. Refresh server-side credentials.'
        elif 'executable' in error or 'exec plugin' in error:
            message = 'Kubeconfig authentication plugin is missing or failed.'
        else:
            message = 'kubectl failed. Check server-side kubeconfig, credentials and network access.'
        raise ClusterError(message)
    return result.stdout


def contexts():
    return {'contexts': [name for name in run(['config', 'get-contexts', '-o', 'name']).splitlines() if name]}


def context_args(context):
    if not isinstance(context, str) or context not in contexts()['contexts']:
        raise ValueError('Select a context available in the server kubeconfig')
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
            continue
        resources.append(available[ns])
        for kind in kinds:
            result = read_json([*args, '--namespace', ns, 'get', kind, '-o', 'json'])
            if not isinstance(result.get('items'), list):
                raise ClusterError('kubectl returned an invalid resource list.')
            resources.extend(result['items'])
    return resources, missing


def live_analyze(body):
    source, target, selected = body['source_context'], body['target_context'], body['namespaces']
    validate_selection(source, target, selected)
    src, src_missing = snapshot(source, selected)
    dst, dst_missing = snapshot(target, selected)
    report = analyze(src, dst, selected, body.get('old_registry', ''), body.get('new_registry', ''))
    report.update(source_context=source, target_context=target,
                  unavailable_kinds={'source': src_missing, 'target': dst_missing},
                  notice='Read-only cluster snapshot. Sequential reads are not atomic. No cluster writes. Secret values are redacted; persistent data is outside scope.')
    return report
