"""Isolated Azure CLI device login. Stdout is a private JSON channel to the web worker."""
import contextlib
import io
import json
import os
import sys


def main():
    channel=sys.stdout
    claims={}
    # Capture only the public challenge and minimal verified identity metadata.
    with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
        from msal import PublicClientApplication
        from azure.cli.core import get_default_cli
        initiate=PublicClientApplication.initiate_device_flow
        acquire=PublicClientApplication.acquire_token_by_device_flow
        def flow(app,*args,**kwargs):
            result=initiate(app,*args,**kwargs)
            if 'user_code' in result:
                channel.write(json.dumps({'state':'pending','verification_uri':result['verification_uri'],
                              'user_code':result['user_code'],'expires_in':result.get('expires_in',900)})+'\n');channel.flush()
            return result
        def token(app,*args,**kwargs):
            result=acquire(app,*args,**kwargs)
            claims.update(result.get('id_token_claims') or {})
            return result
        PublicClientApplication.initiate_device_flow=flow
        PublicClientApplication.acquire_token_by_device_flow=token
        try:code=get_default_cli().invoke(['login','--use-device-code','--tenant',os.environ['MIGRATOR_TENANT_ID'],
                                          '--allow-no-subscriptions','--only-show-errors','--output','none'])
        except BaseException:code=1
    if code not in (0,None) or not claims.get('oid') or not claims.get('tid'):return 1
    channel.write(json.dumps({'state':'authenticated','user':{'object_id':claims['oid'],'tenant_id':claims['tid'],
                  'name':claims.get('name'),'username':claims.get('preferred_username') or claims.get('upn')}})+'\n');channel.flush()
    return 0


if __name__=='__main__':
    try:sys.exit(main())
    except Exception:sys.exit(1)
