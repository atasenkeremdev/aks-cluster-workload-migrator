"""Export selected AKS connection metadata without workstation login credentials."""
import argparse
import json
import os
from pathlib import Path
import subprocess
from migrator.auth import GUID, sanitized_kubeconfig


def export(tenant,contexts,output):
    if not GUID.fullmatch(tenant):raise ValueError('Use an Entra tenant GUID')
    result=subprocess.run(['kubectl','config','view','--raw','--flatten','-o','json'],capture_output=True,check=True,timeout=20)
    config=sanitized_kubeconfig(json.loads(result.stdout),tenant)
    if contexts:
        wanted=set(contexts);found={item['name'] for item in config['contexts']}
        if wanted-found:raise ValueError('Some selected contexts are not present in kubeconfig')
        config['contexts']=[item for item in config['contexts'] if item['name'] in wanted]
        clusters={item['context']['cluster'] for item in config['contexts']};users={item['context']['user'] for item in config['contexts']}
        config['clusters']=[item for item in config['clusters'] if item['name'] in clusters]
        config['users']=[item for item in config['users'] if item['name'] in users]
        config['current-context']=config['contexts'][0]['name']
    descriptor=os.open(Path(output),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w',encoding='utf-8') as stream:json.dump(config,stream,indent=2)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tenant',required=True);parser.add_argument('--context',action='append',default=[])
    parser.add_argument('--output',required=True);args=parser.parse_args()
    try:export(args.tenant,args.context,args.output)
    except Exception:parser.exit(1,'Connection export failed. Check kubectl, selected contexts, tenant GUID and a new writable output path. No credential output was logged.\n')
    print('Credential-free connection metadata exported. Store it as the Deployment kubeconfig Secret.')
