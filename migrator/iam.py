"""Read-only Kubernetes IAM comparison; no credentials or workload manifests are read."""
import json
import re
from datetime import datetime, timezone
from .core import PROTECTED, difference_paths
from . import azure_iam
from .kubernetes import ClusterError, context_args, read_json, progress

RESOURCES = {'ServiceAccount': 'serviceaccounts', 'Role': 'roles.rbac.authorization.k8s.io',
             'RoleBinding': 'rolebindings.rbac.authorization.k8s.io',
             'ClusterRole': 'clusterroles.rbac.authorization.k8s.io',
             'ClusterRoleBinding': 'clusterrolebindings.rbac.authorization.k8s.io'}
CLUSTER_KINDS = {'ClusterRole', 'ClusterRoleBinding'}
IDENTITY_ANNOTATIONS = ('azure.workload.identity/client-id', 'azure.workload.identity/tenant-id',
                        'azure.workload.identity/service-account-token-expiration')


def validate_request(body):
    azure_iam.validate_ids(body)
    source, target = body['source_context'], body['target_context']
    if any(not isinstance(value, str) or not value.strip() or len(value) > 256 for value in (source, target)):
        raise ValueError('Select source and target kubeconfig contexts')
    if source == target:
        raise ValueError('Source and target contexts must be distinct')
    selected = body.get('namespaces', [])
    if not isinstance(selected, list) or len(selected) > 200 or any(
            not isinstance(ns, str) or len(ns) > 63 or not re.fullmatch(r'[a-z0-9](?:[-a-z0-9]*[a-z0-9])?', ns) or ns in PROTECTED
            for ns in selected):
        raise ValueError('Select up to 200 valid, non-excluded namespaces, or leave the list empty for all')
    if len(set(selected)) != len(selected):
        raise ValueError('Namespace names must be unique')


def canonical(value):
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in value.items()}
    if isinstance(value, list):
        return sorted((canonical(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    return value


def inventory_entry(obj, kind):
    """Project an explicit allowlist: never return token secrets or arbitrary annotations."""
    meta = obj['metadata']
    entry = {'kind': kind, 'name': meta['name'], 'namespace': '' if kind in CLUSTER_KINDS else meta['namespace']}
    if kind == 'ServiceAccount':
        entry['configuration'] = {'automountServiceAccountToken': obj.get('automountServiceAccountToken', True),
                                  'workload_identity': {key: meta.get('annotations', {})[key]
                                                        for key in IDENTITY_ANNOTATIONS if key in meta.get('annotations', {})}}
    elif kind in ('Role', 'ClusterRole'):
        entry['configuration'] = {'rules': obj.get('rules', []) or []}
        if obj.get('aggregationRule'):
            entry['configuration']['aggregationRule'] = obj['aggregationRule']
    else:
        entry['configuration'] = {'roleRef': obj['roleRef'], 'subjects': obj.get('subjects', []) or []}
    entry['configuration'] = canonical(entry['configuration'])
    entry['system_managed'] = meta['name'].startswith('system:') or meta.get('labels', {}).get('kubernetes.io/bootstrapping') == 'rbac-defaults'
    return entry


def collect(context, selected):
    entries, coverage = {}, []
    try:
        args = context_args(context)
    except ClusterError as error:
        return {}, [{'kind': kind, 'scope': scope, 'state': 'unavailable', 'count': 0, 'error': str(error)}
                    for kind in RESOURCES for scope in ([''] if kind in CLUSTER_KINDS else (selected or ['*']))]
    for kind, resource in RESOURCES.items():
        scopes = [''] if kind in CLUSTER_KINDS else (selected or ['*'])
        for scope in scopes:
            progress(f'IAM inventory: {context} / {kind} / {scope or "cluster"}')
            flags = [] if not scope else ['--all-namespaces'] if scope == '*' else ['-n', scope]
            check = {'kind': kind, 'scope': scope, 'state': 'complete', 'count': 0}
            try:
                response = read_json([*args, *flags, 'get', resource, '-o', 'json'])
                if not isinstance(response.get('items'), list):
                    raise ClusterError('Kubernetes returned an invalid IAM resource list.')
                for obj in response['items']:
                    meta = obj.get('metadata', {})
                    ns = meta.get('namespace', '')
                    if ns in PROTECTED or (selected and kind not in CLUSTER_KINDS and ns not in selected):
                        continue
                    entry = inventory_entry(obj, kind)
                    entries[(kind, entry['namespace'], entry['name'])] = entry
                    check['count'] += 1
            except ClusterError as error:
                check.update(state='unavailable', error=str(error))
            coverage.append(check)
    return entries, coverage


def analyze(body):
    validate_request(body)
    selected = body.get('namespaces', [])
    snapshots = {}
    coverage = {}
    for side in ('source', 'target'):
        snapshots[side], coverage[side] = collect(body[side + '_context'], selected)
    if body.get('include_azure'):
        for side in ('source', 'target'):
            client_ids = {entry['configuration']['workload_identity'].get('azure.workload.identity/client-id', '').lower()
                          for entry in snapshots[side].values() if entry['kind'] == 'ServiceAccount'} - {''}
            azure_entries, azure_coverage = azure_iam.collect(body['azure_' + side + '_id'], client_ids)
            if any(check['kind'] == 'ServiceAccount' and check['state'] != 'complete' for check in coverage[side]):
                for check in azure_coverage:
                    if check['kind'] in ('ManagedIdentity', 'FederatedCredential', 'AzureRoleAssignment'):
                        check.update(state='unavailable', error='ServiceAccount discovery was incomplete; related workload identities and assignments may be missing.')
            for entry in azure_entries.values():
                entry['configuration'] = canonical(entry['configuration'])
            snapshots[side].update(azure_entries); coverage[side].extend(azure_coverage)
    rows = []
    summary = {key: 0 for key in ('MATCH', 'DIFF', 'SOURCE_ONLY', 'TARGET_ONLY', 'UNKNOWN')}
    for key in sorted(snapshots['source'].keys() | snapshots['target'].keys()):
        source, target = (snapshots[side].get(key) for side in ('source', 'target'))
        unknown = any(not any(check['kind'] == key[0] and check['scope'] in (key[1], '*') and check['state'] == 'complete'
                              for check in coverage[side]) for side in ('source', 'target'))
        status = 'UNKNOWN' if unknown else 'TARGET_ONLY' if source is None else 'SOURCE_ONLY' if target is None else 'MATCH' if source['configuration'] == target['configuration'] else 'DIFF'
        summary[status] += 1
        rows.append({'provider': 'Azure' if key[0] in azure_iam.KINDS else 'Kubernetes', 'kind': key[0], 'namespace': key[1], 'name': key[2], 'status': status,
                     'source': source, 'target': target,
                     'diff_paths': difference_paths(source['configuration'], target['configuration']) if source and target else [],
                     'recommendation': 'Resolve inventory access before deciding' if unknown else 'System-managed RBAC; review cluster defaults instead of copying blindly' if (source or target).get('system_managed') else 'Review target-only access; do not delete automatically' if status == 'TARGET_ONLY' else 'Review target identity and permissions before syncing' if status in ('DIFF', 'SOURCE_ONLY') else 'No configuration difference found'})
    return {'generated_at': datetime.now(timezone.utc).isoformat(), 'resources': rows, 'summary': summary, 'coverage': coverage,
            'complete': all(check['state'] == 'complete' for checks in coverage.values() for check in checks),
            'source_context': body['source_context'], 'target_context': body['target_context'],
            'namespaces': selected, 'include_azure': body.get('include_azure', False),
            'azure_source_id': body.get('azure_source_id') if body.get('include_azure') else None,
            'azure_target_id': body.get('azure_target_id') if body.get('include_azure') else None, 'excluded_namespaces': sorted(PROTECTED),
            'notice': 'Read-only IAM inventory. Kubernetes coverage: RBAC and ServiceAccount Workload Identity annotations, excluding protected namespaces. Optional Azure coverage: AKS access/identity profiles, cluster-scope and inherited role assignments, related managed identities, federated credentials and related principal assignments within each AKS subscription. Cross-subscription identities/assignments, transitive group access, deny assignments, PIM eligibility, custom role definitions, effective access and workload configuration are not verified. Identity IDs and issuers may legitimately differ; do not copy assignments blindly. No IAM resources are modified. Missing read access is unknown, never proof of missing target configuration.'}
