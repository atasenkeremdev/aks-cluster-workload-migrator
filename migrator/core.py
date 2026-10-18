"""Shared migration policy and pure manifest transformations."""
import copy
import hashlib
import json

KINDS = {'Namespace', 'Deployment', 'StatefulSet', 'DaemonSet', 'CronJob',
         'Service', 'ConfigMap', 'Secret', 'ServiceAccount', 'Role', 'RoleBinding',
         'PodDisruptionBudget', 'HorizontalPodAutoscaler', 'NetworkPolicy',
         'ActiveMQArtemis'}
PROTECTED = {'elastic-agent', 'kube-system', 'kube-public', 'kube-node-lease'}
RUNTIME = ('uid', 'resourceVersion', 'creationTimestamp', 'managedFields', 'generation', 'ownerReferences')
ANNOTATIONS = ('kubectl.kubernetes.io/last-applied-configuration', 'deployment.kubernetes.io/revision')


def reason(obj):
    meta = obj.get('metadata', {})
    name, ns = meta.get('name', ''), meta.get('namespace', '')
    if ns in PROTECTED or (obj.get('kind') == 'Namespace' and name in PROTECTED):
        return 'Protected namespace'
    if obj.get('kind') not in KINDS:
        return 'Outside migration scope (Ingress is managed by Terraform)'
    if name == 'kube-root-ca.crt' or name.startswith(('envoy-bootstrap-config-', 'sh.helm.release.')):
        return 'Generated resource'
    if ns.startswith('dbpcorp-postgre-') and name.startswith('postgre-'):
        return 'Excluded PostgreSQL resource'
    if obj.get('kind') == 'Secret' and obj.get('type') == 'kubernetes.io/service-account-token':
        return 'Cluster-specific service account token'
    return None


def clean_metadata(obj):
    meta = obj.get('metadata', {})
    for key in RUNTIME:
        meta.pop(key, None)
    annotations = meta.get('annotations', {})
    for key in ANNOTATIONS:
        annotations.pop(key, None)
    if not annotations:
        meta.pop('annotations', None)
    obj.pop('status', None)


def normalize(obj, old_registry='', new_registry=''):
    obj = copy.deepcopy(obj)
    clean_metadata(obj)
    kind, spec = obj.get('kind'), obj.get('spec', {})
    if kind == 'Service':
        # Preserve headless semantics while dropping allocated addresses.
        if spec.get('clusterIP') != 'None':
            spec.pop('clusterIP', None)
            spec.pop('clusterIPs', None)
        for key in ('ipFamilies', 'ipFamilyPolicy', 'healthCheckNodePort'):
            spec.pop(key, None)
    if kind == 'ServiceAccount':
        obj.pop('secrets', None)
    if kind == 'StatefulSet':
        for claim in spec.get('volumeClaimTemplates', []):
            clean_metadata(claim)
    template = spec.get('jobTemplate', {}).get('spec', {}).get('template', {}) if kind == 'CronJob' else spec.get('template', {})
    for field in ('labels', 'annotations'):
        meta = template.get('metadata', {})
        if field in meta:
            meta[field] = {k: v for k, v in meta[field].items() if not k.startswith('openservicemesh.io/')}
            if not meta[field]:
                meta.pop(field)

    def rewrite(value):
        if isinstance(value, dict):
            return {k: rewrite(v) for k, v in value.items()}
        if isinstance(value, list):
            return [rewrite(v) for v in value]
        if isinstance(value, str) and old_registry and new_registry:
            return value.replace(old_registry, new_registry)
        return value
    return rewrite(obj)


def items(value):
    result = value.get('items', []) if isinstance(value, dict) and value.get('kind') == 'List' else value if isinstance(value, list) else [value]
    if not isinstance(result, list):
        raise ValueError('Expected a JSON manifest, array or Kubernetes List')
    seen = set()
    for obj in result:
        if not isinstance(obj, dict) or not isinstance(obj.get('metadata'), dict) or not obj['metadata'].get('name') or not obj.get('kind') or not obj.get('apiVersion'):
            raise ValueError('Each resource needs apiVersion, kind and metadata.name')
        key = identity(obj)
        if key in seen:
            raise ValueError('Duplicate resource identity')
        seen.add(key)
    return result


def identity(obj):
    meta = obj['metadata']
    return (obj['apiVersion'].split('/')[0] if '/' in obj['apiVersion'] else '', obj['kind'], meta.get('namespace', ''), meta['name'])


def safe_manifest(obj):
    obj = copy.deepcopy(obj)
    if obj.get('kind') == 'Secret':
        for key in ('data', 'stringData'):
            if key in obj:
                obj[key] = {name: '[REDACTED]' for name in obj[key]}
    return obj


def analyze(source, target, namespaces, old_registry='', new_registry=''):
    if not namespaces or any(ns in PROTECTED for ns in namespaces):
        raise ValueError('Select at least one non-protected namespace')
    selected = set(namespaces)
    skipped = []
    def index(resources, side):
        out = {}
        for obj in items(resources):
            meta = obj['metadata']
            ns = meta.get('namespace', meta['name'] if obj['kind'] == 'Namespace' else '')
            why = reason(obj) or ('Outside selected namespaces' if ns not in selected else None)
            if why:
                skipped.append({'side': side, 'kind': obj['kind'], 'name': meta['name'], 'reason': why})
                continue
            out[identity(obj)] = normalize(obj, old_registry, new_registry)
        return out
    src, dst = index(source, 'source'), index(target, 'target')
    rows = []
    for key in sorted(src.keys() | dst.keys()):
        a, b = src.get(key), dst.get(key)
        status = 'TARGET_ONLY' if a is None else 'SOURCE_ONLY' if b is None else 'MATCH' if a == b else 'DIFF'
        rows.append({'kind': key[1], 'namespace': key[2], 'name': key[3], 'status': status,
                     'manifest': safe_manifest(a) if a else None})
    return {'resources': rows, 'skipped': skipped, 'summary': {s: sum(r['status'] == s for r in rows) for s in ('MATCH', 'DIFF', 'SOURCE_ONLY', 'TARGET_ONLY')},
            'notice': 'Offline manifest comparison only. No cluster writes. Secret values are redacted. Volumes and database contents are not migrated.'}
