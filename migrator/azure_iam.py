"""Azure CLI transport and read-only context lookup. Never changes the active account."""
import json
from . import auth
import re
import shutil
import subprocess
from .kubernetes import ClusterError, progress

AKS_ID = re.compile(r'/subscriptions/([0-9a-f-]{36})/resourceGroups/([^/\s]+)/providers/Microsoft\.ContainerService/managedClusters/([^/\s]+)', re.I)


def validate_ids(body):
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
                                  stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, **({'env':auth.command_env()} if auth.enabled() else {}))
    except subprocess.TimeoutExpired:
        raise ClusterError('Azure IAM read timed out. Check Azure connectivity and authentication.') from None
    except OSError:
        raise ClusterError('Unable to start Azure CLI.', 503) from None
    if response.returncode:
        # CLI stderr may contain credentials or authentication plugin output.
        error = response.stderr.lower()
        if 'authorizationfailed' in error or 'forbidden' in error:
            message = 'Azure denied IAM access. Check permissions at the requested scope.'
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
