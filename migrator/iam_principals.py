"""Optional directory name lookup and readable principal-level IAM summaries."""
import json
import re
from urllib.parse import urlsplit
from . import azure_iam
from .kubernetes import ClusterError, progress

GUID=re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',re.I)
GRAPH_HOSTS={'graph.microsoft.com','graph.microsoft.us','dod-graph.microsoft.us','microsoftgraph.chinacloudapi.cn'}


def resolve(rows,source_scope,target_scope):
    identities={}
    by_subscription={}
    for row in rows:
        principal=row['principal_id']
        assignments=(row['source'] or row['target'])['assignments']
        kind=assignments[0].get('principalType') or 'Unknown'
        identities.setdefault(principal,{'principal_id':principal,'display_name':principal,'principal_type':kind,
                                        'name_resolved':False,'resolution_status':'Directory name unavailable; object ID retained'})
        scope=source_scope if row['source'] else target_scope
        if GUID.fullmatch(principal):by_subscription.setdefault(scope.split('/')[2],set()).add(principal)
    if not identities:return identities
    try:
        resource=azure_iam.run_json(['cloud','show','--query','endpoints.microsoftGraphResourceId'])
        endpoint=urlsplit(resource) if isinstance(resource,str) else None
        if (not endpoint or endpoint.scheme!='https' or endpoint.hostname not in GRAPH_HOSTS or endpoint.username or
                endpoint.password or endpoint.query or endpoint.fragment or endpoint.path not in ('','/')):
            raise ClusterError('Directory metadata endpoint is unavailable')
    except (ClusterError,ValueError):return identities
    for sub,principals in by_subscription.items():
        ordered=sorted(principals)
        for start in range(0,len(ordered),1000):
            ids=ordered[start:start+1000]
            progress('Resolving IAM identity display names')
            try:
                response=azure_iam.run_json(['rest','--method','POST','--url',resource.rstrip('/')+'/v1.0/directoryObjects/getByIds',
                    '--resource',resource,'--subscription',sub,'--body',json.dumps({'ids':ids})])
                if not isinstance(response,dict) or not isinstance(response.get('value'),list):raise ClusterError('Directory metadata unavailable')
                for item in response['value']:
                    if not isinstance(item,dict):continue
                    principal=str(item.get('id','')).lower()
                    if principal not in ids:continue
                    name=item.get('displayName')
                    if not isinstance(name,str) or not name.strip():continue
                    kind={'#microsoft.graph.user':'User','#microsoft.graph.group':'Group','#microsoft.graph.servicePrincipal':'ServicePrincipal'}.get(str(item.get('@odata.type','')),identities[principal]['principal_type'])
                    if kind=='ServicePrincipal' and item.get('servicePrincipalType')=='ManagedIdentity':kind='ManagedIdentity'
                    identities[principal].update(display_name=name.strip(),principal_type=kind,name_resolved=True,resolution_status='Directory name resolved')
                    if kind=='User' and isinstance(item.get('userPrincipalName'),str):identities[principal]['user_principal_name']=item['userPrincipalName']
            except ClusterError:
                # Name lookup is optional: never change role coverage or grant eligibility.
                continue
    return identities


def summarize(rows,identities,source_context,target_context):
    grouped={}
    for row in rows:grouped.setdefault(row['principal_id'],[]).append(row)
    result=[]
    for principal,grants in grouped.items():
        identity=identities[principal]
        roles=lambda predicate:sorted({row['name'] for row in grants if predicate(row)})
        source=roles(lambda row:bool(row['source']));target=roles(lambda row:bool(row['target']))
        missing=roles(lambda row:row['status']=='SOURCE_ONLY');different=roles(lambda row:row['status']=='DIFF')
        extra=roles(lambda row:row['status']=='TARGET_ONLY');unknown=roles(lambda row:row['status']=='UNKNOWN')
        label={'ServicePrincipal':'Service principal','ManagedIdentity':'Managed identity','Unknown':'Identity'}.get(identity['principal_type'],identity['principal_type'])
        display=identity['display_name']
        if identity.get('user_principal_name') and identity['user_principal_name']!=display:display+=' ('+identity['user_principal_name']+')'
        text=f"{label} {display}: observed roles on {source_context}: {', '.join(source) or 'none'}. "
        if unknown:text+=f"Comparison with {target_context} is incomplete; missing roles cannot be confirmed for: {', '.join(unknown)}. "
        if missing:text+=f"Missing role assignments on {target_context}: {', '.join(missing)}. "
        elif different:text+='Role IDs exist on both clusters, but grant details differ. '
        elif source and not unknown:text+=f"No source role assignments are missing on {target_context}. "
        if target:text+=f"Observed roles on {target_context}: {', '.join(target)}. "
        if different:text+=f"Conditions or permissions differ for: {', '.join(different)}. "
        if extra:text+=f"Target-only role assignments: {', '.join(extra)}. "
        if not identity['name_resolved']:text+='Display name could not be resolved; the object ID is shown.'
        result.append({**identity,'source_roles':source,'target_roles':target,'missing_roles':missing,
                       'different_roles':different,'target_only_roles':extra,'unverified_roles':unknown,'summary':text.strip()})
    return sorted(result,key=lambda item:(item['principal_type'],item['display_name'].casefold(),item['principal_id']))
