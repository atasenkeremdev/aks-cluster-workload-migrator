"""Read-only Azure CLI IAM adapter. Never changes the active account or requests tokens."""
import json
import re
import shutil
import subprocess
from .kubernetes import ClusterError, progress

KINDS = ('AKSAccessProfile', 'AKSIdentity', 'ManagedIdentity', 'FederatedCredential', 'AzureRoleAssignment')
AKS_ID = re.compile(r'/subscriptions/([0-9a-f-]{36})/resourceGroups/([^/\s]+)/providers/Microsoft\.ContainerService/managedClusters/([^/\s]+)', re.I)


def validate_ids(body):
    if not isinstance(body.get('include_azure', False), bool):
        raise ValueError('Azure inventory selection must be true or false')
    if body.get('include_azure'):
        for side in ('source', 'target'):
            value = body.get('azure_' + side + '_id', '')
            if not isinstance(value, str) or not AKS_ID.fullmatch(value):
                raise ValueError('Enter full source and target AKS resource IDs for Azure IAM inventory')
        if body['azure_source_id'].lower() == body['azure_target_id'].lower():
            raise ValueError('Source and target AKS resource IDs must be distinct')


def run_json(args):
    executable = shutil.which('az')
    if not executable:
        raise ClusterError('Azure CLI is not installed on the application host.', 503)
    progress('Azure IAM: ' + ' '.join(args[:3]), 'waiting')
    try:
        response = subprocess.run([executable, *args, '--only-show-errors', '--output', 'json'],
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise ClusterError('Azure IAM read timed out. Check Azure connectivity and authentication.') from None
    except OSError:
        raise ClusterError('Unable to start Azure CLI.', 503) from None
    if response.returncode:
        # CLI stderr may contain credentials or authentication plugin output.
        error = response.stderr.lower()
        if 'authorizationfailed' in error or 'forbidden' in error:
            message = 'Azure denied IAM inventory access. Check read permissions at the requested scope.'
        elif any(word in error for word in ('az login', 'expired', 'authentication', 'aadsts')):
            message = 'Azure authentication is unavailable or expired. Authenticate Azure CLI on the application host.'
        elif 'not found' in error or 'resourcenotfound' in error:
            message = 'Azure IAM resource was not found at the supplied scope.'
        else:
            message = 'Azure IAM read failed. Check Azure CLI version, authentication, network access and scope permissions.'
        raise ClusterError(message)
    try:
        result = json.loads(response.stdout)
    except (ValueError, TypeError):
        raise ClusterError('Azure CLI returned invalid JSON for IAM inventory.') from None
    progress('Azure IAM read completed', 'completed')
    return result


def collect(aks_id, client_ids):
    subscription = AKS_ID.fullmatch(aks_id).group(1)
    entries, coverage = {}, []
    checks = {kind: {'kind': kind, 'scope': '', 'state': 'complete', 'count': 0} for kind in KINDS}
    def fail(kind, message):
        checks[kind].update(state='unavailable', error=message)
    def add(kind, name, config):
        entries[(kind, '', name)] = {'kind': kind, 'namespace': '', 'name': name, 'configuration': config, 'system_managed': False}
        checks[kind]['count'] += 1
    try:
        aks = run_json(['aks', 'show', '--ids', aks_id, '--subscription', subscription])
        if not isinstance(aks, dict):
            raise ClusterError('Azure returned an invalid AKS profile.')
    except ClusterError as error:
        return {}, [{**check, 'state': 'unavailable', 'error': str(error)} for check in checks.values()]
    aad = aks.get('aadProfile') or {}
    add('AKSAccessProfile', 'access-profile', {'enableRbac': aks.get('enableRbac'),
        'aadProfile': {key: aad[key] for key in ('managed', 'enableAzureRbac', 'tenantId', 'adminGroupObjectIDs') if key in aad},
        'oidcIssuer': (aks.get('oidcIssuerProfile') or {}).get('issuerURL'),
        'workloadIdentityEnabled': ((aks.get('securityProfile') or {}).get('workloadIdentity') or {}).get('enabled')})
    principal_labels = {}
    identity_ids = set()
    identity = aks.get('identity') or {}
    add('AKSIdentity', 'control-plane', {key: identity[key] for key in ('type', 'principalId', 'tenantId') if key in identity})
    if identity.get('principalId'):
        principal_labels[identity['principalId'].lower()] = 'control-plane'
    for identity_id, values in (identity.get('userAssignedIdentities') or {}).items():
        identity_ids.add(identity_id.lower())
        if values.get('principalId'):
            principal_labels[values['principalId'].lower()] = 'control-plane:' + identity_id.rsplit('/', 1)[-1]
    for slot, values in (aks.get('identityProfile') or {}).items():
        add('AKSIdentity', slot, {key: values[key] for key in ('resourceId', 'clientId', 'objectId') if key in values})
        if values.get('resourceId'):
            identity_ids.add(values['resourceId'].lower())
        if values.get('objectId'):
            principal_labels[values['objectId'].lower()] = slot
    # Client IDs are matched within the AKS subscription. Cross-subscription identities need separate review.
    identities = []
    try:
        result = run_json(['identity', 'list', '--subscription', subscription])
        if not isinstance(result, list):
            raise ClusterError('Azure returned an invalid managed identity list.')
        identities = [item for item in result if item.get('id', '').lower() in identity_ids or item.get('clientId', '').lower() in client_ids]
        found = {item.get('clientId', '').lower() for item in identities}
        if client_ids - found:
            fail('ManagedIdentity', 'Some ServiceAccount client IDs could not be resolved in the AKS subscription. They may belong to another subscription or identity type.')
            fail('FederatedCredential', 'Federated credentials for unresolved client IDs were not inventoried.')
            fail('AzureRoleAssignment', 'Related role assignments for unresolved client IDs may be missing.')
        if len(identities) > 200:
            raise ClusterError('More than 200 related managed identities were found. Narrow the Kubernetes namespace scope.')
        if identity_ids - {item.get('id', '').lower() for item in identities}:
            fail('ManagedIdentity', 'Some AKS managed identities were not found in the AKS subscription; cross-subscription identities are not resolved.')
            fail('FederatedCredential', 'Federated credential discovery for AKS managed identities was incomplete.')
            fail('AzureRoleAssignment', 'Some AKS managed identities were unresolved; related principal assignments may be missing.')
        for item in identities:
            identity_id = item['id']
            # Azure CLI versions may omit the derived resourceGroup convenience property.
            match = re.search(r'/resourceGroups/([^/]+)/', identity_id, re.I)
            resource_group = item.get('resourceGroup') or (match.group(1) if match else None)
            if not resource_group:
                fail('ManagedIdentity', 'A managed identity resource group could not be resolved.')
                fail('FederatedCredential', 'Federated credential discovery for a managed identity was incomplete.')
                continue
            # Include resource group in the row key: identities with the same name can coexist.
            name = resource_group + '/' + item['name']
            add('ManagedIdentity', name, {key: item[key] for key in ('id', 'clientId', 'principalId', 'tenantId') if key in item})
            if item.get('principalId'):
                principal_labels.setdefault(item['principalId'].lower(), 'managed:' + name)
            try:
                fics = run_json(['identity', 'federated-credential', 'list', '--identity-name', item['name'],
                                '--resource-group', resource_group, '--subscription', subscription])
                if not isinstance(fics, list):
                    raise ClusterError('Azure returned an invalid federated credential list.')
                for fic in fics:
                    add('FederatedCredential', name + '/' + fic['name'], {key: fic[key] for key in ('issuer', 'subject', 'audiences') if key in fic})
            except ClusterError as error:
                fail('FederatedCredential', str(error))
    except ClusterError as error:
        fail('ManagedIdentity', str(error)); fail('FederatedCredential', 'Managed identity discovery was incomplete; federated credential coverage is incomplete.')
        fail('AzureRoleAssignment', 'Managed identity discovery was incomplete; related principal role assignments may be missing.')
    def add_assignments(assignments, location):
        if not isinstance(assignments, list):
            raise ClusterError('Azure returned an invalid role assignment list.')
        for assignment in assignments:
            principal = assignment.get('principalId', '')
            role = assignment.get('roleDefinitionId', '')
            scope = assignment.get('scope', '')
            label = principal_labels.get(principal.lower(), principal.lower())
            scope_key = '<aks-cluster>' if scope.lower() == aks_id.lower() else scope.lower()
            name = location + '/' + label + '/' + role.rsplit('/', 1)[-1] + '/' + scope_key
            config = {key: assignment[key] for key in ('principalId', 'principalType', 'roleDefinitionId', 'scope', 'condition', 'conditionVersion') if key in assignment}
            add('AzureRoleAssignment', name, config)
    common = ['--subscription', subscription, '--fill-principal-name', 'false', '--fill-role-definition-name', 'false']
    try:
        add_assignments(run_json(['role', 'assignment', 'list', '--scope', aks_id, '--include-inherited', *common]), 'cluster-access')
    except ClusterError as error:
        fail('AzureRoleAssignment', str(error))
    if len(principal_labels) > 200:
        fail('AzureRoleAssignment', 'More than 200 related principals were found; their assignments were not scanned.')
    else:
        for principal, label in principal_labels.items():
            try:
                add_assignments(run_json(['role', 'assignment', 'list', '--assignee-object-id', principal, '--all', *common]), 'identity-access')
            except ClusterError as error:
                fail('AzureRoleAssignment', str(error))
    coverage.extend(checks.values())
    return entries, coverage


def resolve_context(body):
    """Match the selected kubeconfig API hostname to Azure AKS endpoint metadata."""
    from urllib.parse import urlsplit
    from .kubernetes import context_args, run
    context = body['context']
    args = context_args(context)
    # Project only the API server URL, never users, certificates or exec configuration.
    server = run([*args, 'config', 'view', '--minify', '-o', 'jsonpath={.clusters[0].cluster.server}']).strip()
    try:
        url = urlsplit(server)
        hostname = url.hostname.lower().rstrip('.') if url.hostname else None
        valid = url.scheme == 'https' and not url.username and not url.password and hostname
    except ValueError:
        valid = False
    if not valid:
        return {'context': context, 'resource_id': None, 'message': 'The context has no usable HTTPS API endpoint. Enter the AKS resource ID manually.'}
    subscriptions = run_json(['account', 'list', '--query', "[?state=='Enabled'].id"])
    if not isinstance(subscriptions, list) or any(not isinstance(item, str) for item in subscriptions):
        raise ClusterError('Azure returned an invalid subscription list.')
    subscriptions = sorted(set(subscriptions))
    if not subscriptions or len(subscriptions) > 20:
        return {'context': context, 'resource_id': None, 'message': 'Automatic lookup requires 1–20 accessible enabled subscriptions. Enter the AKS resource ID manually.'}
    matches = {}
    incomplete = False
    for subscription in subscriptions:
        try:
            clusters = run_json(['aks', 'list', '--subscription', subscription, '--query', '[].{id:id,fqdn:fqdn,privateFqdn:privateFqdn}'])
            if not isinstance(clusters, list):
                raise ClusterError('Azure returned an invalid AKS list.')
            for cluster in clusters:
                resource_id = cluster.get('id', '')
                endpoints = {value.lower().rstrip('.') for key in ('fqdn', 'privateFqdn')
                             if isinstance(value := cluster.get(key), str) and value}
                if hostname in endpoints and isinstance(resource_id, str) and AKS_ID.fullmatch(resource_id):
                    matches[resource_id.lower()] = resource_id
        except ClusterError:
            incomplete = True
    if len(matches) == 1 and not incomplete:
        return {'context': context, 'resource_id': next(iter(matches.values())), 'message': 'AKS resource ID matched to the selected context API endpoint.'}
    message = ('Some subscriptions could not be read.' if incomplete else
               'Multiple AKS resources match the context endpoint.' if len(matches) > 1 else
               'No accessible AKS resource matches the context endpoint.')
    return {'context': context, 'resource_id': None, 'message': message + ' Enter the AKS resource ID manually.'}
