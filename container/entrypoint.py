"""Copy mounted connection config privately; the server strips all user credentials."""
import os
from pathlib import Path
import sys


def prepare():
    Path(os.environ.get('MIGRATOR_STATE_DIR','/var/lib/migrator')).mkdir(parents=True,exist_ok=True,mode=0o700)
    source=Path(os.environ.get('MIGRATOR_KUBECONFIG_SECRET','/etc/migrator/kubeconfig/config'))
    target=Path(os.environ.get('KUBECONFIG','/tmp/kubeconfig/config'))
    if source.resolve()==target.resolve():raise ValueError('Mounted and runtime kubeconfig paths must differ.')
    data=source.read_bytes();target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    descriptor=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(descriptor,'wb') as stream:stream.write(data)
    os.chmod(target,0o600)
    mode=os.environ.get('MIGRATOR_AZURE_AUTH','device-code')
    auth_mode=os.environ.get('MIGRATOR_AUTH_MODE','local')
    if (mode,auth_mode) not in (('device-code','device-code'),('none','local')):
        raise ValueError('Use device-code user authentication, or explicitly select local/none for private offline testing.')


if __name__=='__main__':
    try:prepare()
    except Exception:
        print('Container initialization failed. Check the kubeconfig mount, writable volumes and authentication configuration.',file=sys.stderr)
        sys.exit(1)
    os.execv(sys.executable,[sys.executable,'-m','migrator.server',*sys.argv[1:]])
