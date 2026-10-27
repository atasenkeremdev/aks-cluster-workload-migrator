"""Azure IAM role assignment inventory and explicitly confirmed, additive sync."""
import copy
import hashlib
import json
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from . import azure_iam, iam_history, iam_principals
from .kubernetes import ClusterError

_PLANS = {}
_LOCK = threading.Lock()
GUID = re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', re.I)


def validate_request(body):
    azure_iam.validate_ids(body)
    for side in ('source', 'target'):
        value = body[side + '_context']
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise ValueError('Select source and target contexts')
    if body['source_context'] == body['target_context']:
        raise ValueError('Source and target contexts must be distinct')


def canonical(value):
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in value.items()}
    if isinstance(value, list):
        return sorted((canonical(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(canonical(value), sort_keys=True).encode()).hexdigest()


def subscription(scope):
    return azure_iam.AKS_ID.fullmatch(scope).group(1)


def collect(scope):
    """Read assignments and role definitions only. Never contact Kubernetes APIs."""
    result = {'assignments': [], 'definitions': {}, 'coverage': [], 'scope': scope}
    sub = subscription(scope)
    for kind in ('AzureRoleAssignment', 'AzureRoleDefinition'):
        check = {'kind': kind, 'scope': scope, 'state': 'complete', 'count': 0}
        try:
            if kind == 'AzureRoleAssignment':
                values = azure_iam.run_json(['role', 'assignment', 'list', '--scope', scope, '--include-inherited',
                                            '--subscription', sub, '--fill-principal-name', 'false', '--fill-role-definition-name', 'false'])
            else:
                values = azure_iam.run_json(['role', 'definition', 'list', '--scope', scope, '--subscription', sub])
            if not isinstance(values, list):
                raise ClusterError('Azure returned an invalid role inventory list.')
            for value in values:
                if kind == 'AzureRoleAssignment':
                    entry = {key: value.get(key) for key in ('id', 'principalId', 'principalType', 'roleDefinitionId', 'scope', 'condition', 'conditionVersion')}
                    if any(not isinstance(entry[key], str) or not entry[key] for key in ('id', 'principalId', 'roleDefinitionId', 'scope')):
                        raise ClusterError('Azure returned an incomplete role assignment.')
                    # Ignore child namespace scopes; this workflow compares access to the AKS resource itself.
                    assignment_scope = entry['scope'].lower().rstrip('/')
                    if assignment_scope != scope.lower() and not scope.lower().startswith(assignment_scope + '/') and not assignment_scope.startswith('/providers/microsoft.management/managementgroups/'):
                        continue
                    entry['inherited'] = assignment_scope != scope.lower()
                    result['assignments'].append(entry)
                else:
                    entry = {key: value.get(key) for key in ('id', 'roleName', 'roleType', 'assignableScopes', 'permissions')}
                    if (not isinstance(entry['id'], str) or not entry['id'] or not isinstance(entry['roleName'], str) or
                            not isinstance(entry['permissions'], list) or not all(isinstance(item, dict) for item in entry['permissions']) or
                            not isinstance(entry['assignableScopes'], list) or not all(isinstance(item, str) for item in entry['assignableScopes'])):
                        raise ClusterError('Azure returned an incomplete role definition.')
                    result['definitions'][entry['id'].rsplit('/', 1)[-1].lower()] = entry
                check['count'] += 1
        except ClusterError as error:
            check.update(state='unavailable', error=str(error))
        result['coverage'].append(check)
    return result


def role_key(assignment):
    return assignment['principalId'].lower(), assignment['roleDefinitionId'].rsplit('/', 1)[-1].lower()


def semantic(assignment, definition):
    return {'condition': assignment.get('condition') or '', 'conditionVersion': assignment.get('conditionVersion') or '',
            'principalType': assignment.get('principalType') or '',
            'permissions': canonical(definition.get('permissions') or [])}


def grant_set(assignments, definition):
    return {json.dumps(semantic(item, definition), sort_keys=True) for item in assignments}


def complete(snapshot):
    return all(check['state'] == 'complete' for check in snapshot['coverage'])


def grouped(snapshot):
    groups = {}
    for assignment in snapshot['assignments']:
        groups.setdefault(role_key(assignment), []).append(assignment)
    return groups


def eligible(source, definition, target_definition):
    if not source:
        return 'Source assignment no longer exists'
    if any(item['inherited'] for item in source):
        return 'Inherited roles must be managed at their parent scope'
    if len(source) != 1 or source[0].get('condition') or source[0].get('conditionVersion'):
        return 'Conditional or overlapping source grants require manual review'
    if source[0].get('principalType') not in ('User', 'Group', 'ServicePrincipal'):
        return 'Principal type must be resolved before sync'
    if not definition or not target_definition:
        return 'The role definition must be available on both sides'
    if fingerprint(definition.get('permissions')) != fingerprint(target_definition.get('permissions')):
        return 'Source and target role permissions differ; align custom role definitions separately'
    return ''


def analyze(body):
    validate_request(body)
    snapshots = {side: collect(body['azure_' + side + '_id']) for side in ('source', 'target')}
    groups = {side: grouped(value) for side, value in snapshots.items()}
    rows, summary = [], {key: 0 for key in ('MATCH', 'DIFF', 'SOURCE_ONLY', 'TARGET_ONLY', 'UNKNOWN')}
    for key in sorted(groups['source'].keys() | groups['target'].keys()):
        source, target = (groups[side].get(key, []) for side in ('source', 'target'))
        definitions = {side: snapshots[side]['definitions'].get(key[1]) for side in ('source', 'target')}
        unknown = not all(complete(snapshot) for snapshot in snapshots.values()) or any(
            groups[side].get(key) and not definitions[side] for side in ('source', 'target'))
        status = 'UNKNOWN' if unknown else 'TARGET_ONLY' if not source else 'SOURCE_ONLY' if not target else (
            'MATCH' if grant_set(source, definitions['source']) == grant_set(target, definitions['target']) else 'DIFF')
        reason = 'Resolve inventory access before syncing' if unknown else 'Existing target grants are never overwritten or removed' if target else eligible(source, definitions['source'], definitions['target'])
        syncable = status == 'SOURCE_ONLY' and not reason
        definition = definitions['source'] or definitions['target'] or {}
        summary[status] += 1
        rows.append({'provider': 'Azure', 'kind': 'AzureRoleAssignment', 'namespace': '', 'principal_id': key[0], 'role_id': key[1],
                     'name': definition.get('roleName') or key[1], 'status': status, 'syncable': syncable,
                     'source': {'assignments': source, 'definition': definitions['source']} if source else None,
                     'target': {'assignments': target, 'definition': definitions['target']} if target else None,
                     'recommendation': 'Review and create this missing grant on the target AKS resource' if syncable else reason or 'No permission difference found'})
    identities = iam_principals.resolve(rows, body['azure_source_id'], body['azure_target_id'])
    for row in rows:row['identity'] = identities[row['principal_id']]
    principals = iam_principals.summarize(rows, identities, body['source_context'], body['target_context'])
    return {'principals': principals, 'generated_at': datetime.now(timezone.utc).isoformat(), 'resources': rows, 'summary': summary,
            'coverage': {side: snapshot['coverage'] for side, snapshot in snapshots.items()},
            'complete': all(complete(snapshot) for snapshot in snapshots.values()) and not summary['UNKNOWN'],
            **{key: body[key] for key in ('source_context', 'target_context', 'azure_source_id', 'azure_target_id')},
            'notice': 'Azure IAM at the selected AKS resource scopes only. Reader and AKS RBAC roles are compared by principal object ID, role ID, conditions and permissions, not generated assignment IDs or different source/target scope IDs. Inherited grants are visible but are not copied. Kubernetes RBAC, managed identity outbound grants, namespace-level Azure grants, transitive groups, deny assignments and PIM eligibility are not scanned. Sync only adds an explicitly reviewed missing direct grant; no existing grant is changed or deleted.'}


def tenant(scope):
    value = azure_iam.run_json(['account', 'show', '--subscription', subscription(scope), '--query', 'tenantId'])
    if not isinstance(value, str) or not GUID.fullmatch(value):
        raise ClusterError('Azure tenant identity could not be verified.')
    return value.lower()


def validated_grant(body):
    validate_request(body)
    principal, role = body['principal_id'], body['role_id']
    if not isinstance(principal, str) or not GUID.fullmatch(principal) or not isinstance(role, str) or not GUID.fullmatch(role):
        raise ValueError('Select a valid principal and role from the IAM inventory')
    source_scope, target_scope = body['azure_source_id'], body['azure_target_id']
    if tenant(source_scope) != tenant(target_scope):
        raise ValueError('Cross-tenant IAM sync is unsupported; map target principals manually')
    snapshots = {side: collect(body['azure_' + side + '_id']) for side in ('source', 'target')}
    if not all(complete(snapshot) for snapshot in snapshots.values()):
        raise ValueError('Complete source and target role inventory is required before sync')
    key = principal.lower(), role.lower()
    source = grouped(snapshots['source']).get(key, [])
    target = grouped(snapshots['target']).get(key, [])
    source_definition = snapshots['source']['definitions'].get(key[1])
    target_definition = snapshots['target']['definitions'].get(key[1])
    reason = eligible(source, source_definition, target_definition)
    if reason:
        raise ValueError(reason)
    if not source:
        raise ValueError('Source assignment no longer exists')
    scopes = target_definition.get('assignableScopes') or []
    if not any(scope == '/' or target_scope.lower() == scope.lower() or target_scope.lower().startswith(scope.lower().rstrip('/') + '/') for scope in scopes):
        raise ValueError('The target role cannot be assigned at this scope')
    if target:
        same = all(semantic(item, target_definition) == semantic(source[0], source_definition) for item in target)
        if not same:
            raise ValueError('A differing target grant exists. Existing grants are never overwritten')
        return None
    return {'principalId': principal.lower(), 'principalType': source[0]['principalType'], 'roleName': target_definition['roleName'],
            'roleDefinitionId': target_definition['id'], 'targetScope': target_scope, 'sourceScope': source_scope,
            'sourceAssignment': source[0], 'sourceDefinition': source_definition, 'targetDefinition': target_definition}


def sync_plan(body):
    grant = validated_grant(body)
    if grant is None:
        raise ValueError('An equivalent target grant already exists; refresh the inventory')
    token = secrets.token_urlsafe(32)
    with _LOCK:
        for key in list(_PLANS):
            if _PLANS[key]['expires'] < time.monotonic():del _PLANS[key]
        if len(_PLANS) >= 100:raise ClusterError('Too many IAM previews. Try again later.', 429)
        _PLANS[token] = {'body': copy.deepcopy(body), 'grant': grant, 'expires': time.monotonic()+300}
    return {'token': token, 'expires_in': 300, 'grant': {key: grant[key] for key in ('principalId', 'principalType', 'roleName', 'roleDefinitionId', 'targetScope', 'sourceScope')},
            'warning': 'This adds a target Azure IAM permission grant to the same principal. Azure does not provide a role-assignment dry-run; the preview does not prove write authorization. Review the exact principal, role and target scope before confirming.'}


def discard(body):
    token = body.get('token')
    with _LOCK:_PLANS.pop(token, None)
    return {'discarded': True}


def sync_execute(body):
    if body.get('confirm') is not True:raise ValueError('Explicit IAM sync confirmation is required')
    with _LOCK:plan = _PLANS.pop(body.get('token', ''), None)
    if not plan or plan['expires'] < time.monotonic():raise ValueError('IAM preview expired or already used; preview again')
    grant = validated_grant(plan['body'])
    if grant is None:return {'status': 'AlreadyPresent'}
    if fingerprint(grant) != fingerprint(plan['grant']):raise ValueError('IAM configuration changed after preview; scan and preview again')
    assignment_name = str(uuid.uuid5(uuid.NAMESPACE_URL, grant['targetScope'].lower()+'/'+grant['principalId']+'/'+plan['body']['role_id'].lower()))
    record = iam_history.save({'status': 'Applying', 'principal_id': grant['principalId'], 'role': grant['roleName'],
                               'source_scope': grant['sourceScope'], 'target_scope': grant['targetScope'], 'assignment_name': assignment_name})
    try:
        result = azure_iam.run_json(['role', 'assignment', 'create', '--assignee-object-id', grant['principalId'],
            '--assignee-principal-type', grant['principalType'], '--role', grant['roleDefinitionId'], '--scope', grant['targetScope'],
            '--subscription', subscription(grant['targetScope']), '--name', assignment_name])
        if (result.get('principalId', '').lower() != grant['principalId'] or result.get('scope', '').lower() != grant['targetScope'].lower() or
                result.get('roleDefinitionId', '').rsplit('/', 1)[-1].lower() != grant['roleDefinitionId'].rsplit('/', 1)[-1].lower() or result.get('condition') or result.get('conditionVersion')):
            raise ValueError('Unexpected assignment response')
        iam_history.save({**record, 'status': 'Applied'})
    except Exception as error:
        try:iam_history.save({**record, 'status': 'Unknown'})
        except Exception:pass
        message = 'IAM write outcome is uncertain. Inspect the target Azure IAM assignment and IAM history before retrying.'
        if isinstance(error, ClusterError):message += ' ' + str(error)
        raise ClusterError(message) from None
    return {'status': 'Applied', 'operation_id': record['id']}
