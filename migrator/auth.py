"""Tenant-restricted Microsoft device login with isolated Azure CLI user profiles."""
import contextvars
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

current_session=contextvars.ContextVar('migrator_user_session',default=None)
_SESSIONS={}
_LOCK=threading.RLock()
COOKIE='migrator_session'
_KUBECONFIG_PATH=None
GUID=re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',re.I)


class AuthError(Exception):
    def __init__(self,message='Sign in with Microsoft to continue.',status=401):
        super().__init__(message);self.status=status


def enabled():return os.environ.get('MIGRATOR_AUTH_MODE','local')!='local'


def validate_config():
    if not enabled():return
    if os.environ.get('MIGRATOR_AUTH_MODE')!='device-code':raise ValueError('MIGRATOR_AUTH_MODE must be local or device-code')
    if not GUID.fullmatch(os.environ.get('MIGRATOR_TENANT_ID','')):raise ValueError('Set MIGRATOR_TENANT_ID to your Entra tenant GUID')
    origin=os.environ.get('MIGRATOR_PUBLIC_ORIGIN','')
    parsed=urlsplit(origin)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment or
        parsed.scheme not in ('http','https') or (parsed.scheme=='http' and parsed.hostname not in ('localhost','127.0.0.1'))):
        raise ValueError('Set MIGRATOR_PUBLIC_ORIGIN to an HTTPS origin, or an HTTP localhost origin for port-forward')
    if any(not GUID.fullmatch(value) for value in allowed_ids()):raise ValueError('MIGRATOR_ALLOWED_OBJECT_IDS must contain comma-separated user object GUIDs')


def allowed_ids():return {value.strip().lower() for value in os.environ.get('MIGRATOR_ALLOWED_OBJECT_IDS','').split(',') if value.strip()}


def allowed_request(handler):
    if enabled():
        origin=os.environ.get('MIGRATOR_PUBLIC_ORIGIN','')
        return handler.headers.get('Host','')==urlsplit(origin).netloc and handler.headers.get('Origin',origin)==origin
    host=handler.headers.get('Host','')
    return host in (f'127.0.0.1:{handler.server.server_port}',f'localhost:{handler.server.server_port}') and handler.headers.get('Origin','http://'+host)=='http://'+host


def cookie(value,clear=False):
    secure=urlsplit(os.environ.get('MIGRATOR_PUBLIC_ORIGIN','')).scheme=='https'
    return f'{COOKIE}={value}; Path=/; HttpOnly; SameSite=Strict; Max-Age={0 if clear else 28800}'+('; Secure' if secure else '')


def session_id(handler):
    try:
        cookies=SimpleCookie(handler.headers.get('Cookie',''))
        return cookies[COOKIE].value if COOKIE in cookies else None
    except Exception:return None


def session(sid=None,required=True):
    sid=sid or current_session.get()
    with _LOCK:
        value=_SESSIONS.get(sid)
        if not value or value['expires']<=time.time() or value['state']!='authenticated':
            if required:raise AuthError()
            return None
        return value


def owner():
    if not enabled():return None
    value=session();return value['user']['tenant_id']+':'+value['user']['object_id']


def check_owner(record):
    if enabled() and record is not None and record.get('owner')!=owner():raise AuthError('This resource belongs to a different signed-in user.',403)


def bind_owner(record):
    if not enabled():return record
    if record.get('owner') is not None:check_owner(record)
    return {**record,'owner':owner()}


def command_env():
    if not enabled():return None
    value=session()
    if not _KUBECONFIG_PATH:raise AuthError('Signed-in Kubernetes configuration is not initialized.',503)
    result=os.environ.copy();result['AZURE_CONFIG_DIR']=value['profile'];result['KUBECONFIG']=_KUBECONFIG_PATH
    # No shared service-principal session is ever selected in signed-in mode.
    return result


def run_as(sid,callback,*args):
    token=current_session.set(sid)
    try:return callback(*args)
    finally:current_session.reset(token)


def stop_process(process):
    if not process or process.poll() is not None:return
    try:
        process.terminate();process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill();process.wait(timeout=5)
    except OSError:pass


def cleanup(sid):
    with _LOCK:value=_SESSIONS.pop(sid,None)
    if value:
        process=value.get('process')
        stop_process(process)
        shutil.rmtree(value['root'],ignore_errors=True)


def new_session():
    with _LOCK:
        for sid,value in list(_SESSIONS.items()):
            if value['expires']<=time.time():cleanup(sid)
        if len(_SESSIONS)>=50:raise AuthError('Too many active sessions. Try again later.',429)
        root=Path(os.environ.get('MIGRATOR_SESSION_DIR','/tmp/migrator-sessions'))
        root.mkdir(parents=True,exist_ok=True,mode=0o700)
        path=tempfile.mkdtemp(prefix='session-',dir=root)
        sid=secrets.token_urlsafe(32)
        value={'state':'anonymous','root':path,'profile':str(Path(path)/'azure'),'expires':time.time()+600}
        _SESSIONS[sid]=value
        return sid


def start_login(sid):
    with _LOCK:
        value=_SESSIONS.get(sid)
        if not value:raise AuthError('Reload the sign-in page.')
        if value['state'] in ('starting','pending','authenticated'):return
        if sum(item['state'] in ('starting','pending') for item in _SESSIONS.values())>=4:raise AuthError('Sign-in queue is full. Try again later.',429)
        value.pop('user_code',None);value.pop('verification_uri',None);value.pop('error',None);value.pop('user',None)
        value.update(state='starting',expires=time.time()+900)
    stop_process(value.get('process'));shutil.rmtree(value['profile'],ignore_errors=True)
    threading.Thread(target=_login_worker,args=(sid,),daemon=True).start()


def _login_worker(sid):
    with _LOCK:value=_SESSIONS.get(sid)
    if not value:return
    env=os.environ.copy();env['AZURE_CONFIG_DIR']=value['profile'];env['MIGRATOR_TENANT_ID']=os.environ['MIGRATOR_TENANT_ID'];env['AZURE_CORE_LOGIN_EXPERIENCE_V2']='off';env['AZURE_CORE_COLLECT_TELEMETRY']='0'
    Path(value['profile']).mkdir(parents=True,exist_ok=True,mode=0o700)
    process=None
    try:
        helper=Path(__file__).resolve().parent.parent/'container'/'device_login.py'
        process=subprocess.Popen([os.environ.get('MIGRATOR_AZURE_PYTHON','/opt/azure-cli/bin/python'),str(helper)],env=env,
                                 stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
        with _LOCK:
            if sid not in _SESSIONS:stop_process(process);return
            value['process']=process
        for line in process.stdout:
            try:message=json.loads(line)
            except (ValueError,TypeError):continue
            with _LOCK:
                if sid not in _SESSIONS:stop_process(process);return
                if message.get('state')=='pending':
                    parsed=urlsplit(message.get('verification_uri',''))
                    if (parsed.scheme!='https' or parsed.hostname not in ('microsoft.com','www.microsoft.com','login.microsoftonline.com') or
                        parsed.username or parsed.password or parsed.port not in (None,443) or
                        not re.fullmatch(r'[A-Za-z0-9-]{4,32}',str(message.get('user_code','')))):continue
                    value.update(state='pending',verification_uri=message['verification_uri'],user_code=str(message['user_code']),
                                 expires=time.time()+min(int(message.get('expires_in',900)),900))
                elif message.get('state')=='authenticated':
                    user=message.get('user',{})
                    oid=str(user.get('object_id','')).lower();tenant=str(user.get('tenant_id','')).lower()
                    if (not GUID.fullmatch(oid) or tenant!=os.environ['MIGRATOR_TENANT_ID'].lower() or
                        (allowed_ids() and oid not in allowed_ids())):
                        value.update(state='failed',error='This account is not allowed to use this application.');continue
                    value.update(state='authenticated',user={'object_id':oid,'tenant_id':tenant,'name':str(user.get('name') or oid),
                                 'username':str(user.get('username') or '')},expires=time.time()+28800)
                    value.pop('user_code',None);value.pop('verification_uri',None)
        code=process.wait(timeout=5)
        with _LOCK:
            if sid in _SESSIONS and (code or value['state']!='authenticated'):
                value.update(state='failed',error=value.get('error','Microsoft sign-in did not complete. Retry or check your tenant device-code policy.'))
                shutil.rmtree(value['profile'],ignore_errors=True)
    except Exception:
        stop_process(process);shutil.rmtree(value['profile'],ignore_errors=True)
        with _LOCK:
            if sid in _SESSIONS:value.update(state='failed',error='Microsoft sign-in is unavailable. Check Azure CLI, tenant and connectivity.')


def handle(handler):
    """Handle login endpoints. Return True when the request is consumed."""
    if not enabled():return False
    path=handler.path
    if path not in ('/signin','/auth/status','/auth/start','/auth/logout'):return False
    sid=session_id(handler)
    if path=='/signin' and handler.command=='GET':
        if session(sid,False):handler.send_response(302);handler.send_header('Location','/');handler.end_headers();return True
        with _LOCK:exists=sid in _SESSIONS and _SESSIONS[sid]['expires']>time.time()
        if not exists:sid=new_session()
        data=(Path(__file__).resolve().parent.parent/'web'/'signin.html').read_bytes()
        handler.send_response(200);handler.send_header('Content-Type','text/html; charset=utf-8');handler.send_header('Cache-Control','no-store')
        handler.send_header('Set-Cookie',cookie(sid));handler.send_header('Content-Length',str(len(data)));handler.end_headers();handler.wfile.write(data);return True
    if path=='/auth/status' and handler.command=='GET':
        with _LOCK:value=_SESSIONS.get(sid)
        if not value or value['expires']<=time.time():handler.reply(200,{'state':'anonymous'});return True
        handler.reply(200,{key:value[key] for key in ('state','verification_uri','user_code','expires','user','error') if key in value});return True
    if handler.command!='POST':handler.reply(405,{'error':'Method not allowed'});return True
    if handler.headers.get_content_type()!='application/json':handler.reply(415,{'error':'JSON required'});return True
    length=int(handler.headers.get('Content-Length','0'))
    if not 0<length<1024:handler.reply(400,{'error':'Invalid sign-in request'});return True
    json.loads(handler.rfile.read(length))
    if path=='/auth/start':
        with _LOCK:exists=sid in _SESSIONS and _SESSIONS[sid]['expires']>time.time()
        if not exists:sid=new_session()
        start_login(sid);handler.reply(202,{'state':'starting'},{'Set-Cookie':cookie(sid)});return True
    if path=='/auth/logout':cleanup(sid);handler.reply(200,{'state':'anonymous'},{'Set-Cookie':cookie('',True)});return True
    return False


def sanitized_kubeconfig(config,tenant,server_id='6dae42f8-4368-4678-94ff-3960e28e3630'):
    """Retain endpoint/context metadata but discard ALL original user credentials."""
    users={item['name']:item.get('user',{}) for item in config.get('users',[])}
    clusters=[]
    for item in config.get('clusters',[]):
        value=item.get('cluster',{})
        if value.get('insecure-skip-tls-verify'):raise ValueError('TLS verification must remain enabled')
        for key in ('server','proxy-url'):
            url=urlsplit(value.get(key,''))
            if url.username or url.password:raise ValueError('Embedded URL credentials are not supported')
        clusters.append({'name':item['name'],'cluster':{key:value[key] for key in ('server','certificate-authority-data','certificate-authority','tls-server-name','proxy-url','disable-compression') if key in value}})
    result={'apiVersion':'v1','kind':'Config','clusters':clusters,'contexts':[],'users':[],
            'current-context':config.get('current-context','')}
    for index,item in enumerate(config.get('contexts',[])):
        context={key:value for key,value in item['context'].items() if key in ('cluster','user','namespace')};user=users.get(context.get('user'),{})
        audience=server_id
        args=(user.get('exec') or {}).get('args',[])
        if '--server-id' in args:
            offset=args.index('--server-id')
            if offset+1<len(args):audience=str(args[offset+1]).removeprefix('spn:')
        provider=(user.get('auth-provider') or {}).get('config',{})
        audience=str(provider.get('apiserver-id') or audience).removeprefix('spn:')
        name='migrator-user-'+str(index);context['user']=name
        result['contexts'].append({'name':item['name'],'context':context})
        result['users'].append({'name':name,'user':{'exec':{'apiVersion':'client.authentication.k8s.io/v1beta1',
            'command':'kubelogin','args':['get-token','--login','azurecli','--server-id',audience,'--tenant-id',tenant],
            'interactiveMode':'Never'}}})
    return result


def prepare_kubeconfig():
    """Always sanitize in the server too, including direct (non-container) hosting."""
    global _KUBECONFIG_PATH
    if not enabled():return
    try:
        result=subprocess.run(['kubectl','config','view','--raw','--flatten','-o','json'],capture_output=True,check=True,timeout=20)
        config=sanitized_kubeconfig(json.loads(result.stdout),os.environ['MIGRATOR_TENANT_ID'],
                                   os.environ.get('MIGRATOR_AKS_SERVER_ID','6dae42f8-4368-4678-94ff-3960e28e3630'))
        root=Path(os.environ.get('MIGRATOR_SESSION_DIR','/tmp/migrator-sessions'))
        root.mkdir(parents=True,exist_ok=True,mode=0o700)
        descriptor,path=tempfile.mkstemp(prefix='connections-',suffix='.json',dir=root)
        with os.fdopen(descriptor,'w',encoding='utf-8') as stream:json.dump(config,stream)
        _KUBECONFIG_PATH=path
        os.environ['KUBECONFIG']=path
    except Exception:raise ValueError('Unable to prepare a credential-free user kubeconfig. Check kubectl and the mounted kubeconfig.') from None
