"""User login isolation: endpoints, command profiles, ownership and credential stripping."""
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from migrator import auth, history, iam_history, kubernetes, iam, jobs
from migrator.server import Handler

TENANT='11111111-1111-1111-1111-111111111111'
ALICE='22222222-2222-2222-2222-222222222222'
BOB='33333333-3333-3333-3333-333333333333'


class UserAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.env=patch.dict(os.environ,{'MIGRATOR_AUTH_MODE':'device-code','MIGRATOR_TENANT_ID':TENANT,
            'MIGRATOR_ALLOWED_OBJECT_IDS':'','MIGRATOR_PUBLIC_ORIGIN':'http://127.0.0.1:8081',
            'MIGRATOR_SESSION_DIR':self.temp.name+'/sessions','MIGRATOR_STATE_DIR':self.temp.name+'/state',
            'AZURE_CONFIG_DIR':'/SHARED-MUST-NOT-BE-USED'})
        self.env.start();self.addCleanup(self.env.stop)
        self.path_patch=patch.object(auth,'_KUBECONFIG_PATH',self.temp.name+'/sanitized-config')
        self.path_patch.start();self.addCleanup(self.path_patch.stop)
        self.sids=[]
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for sid in self.sids:auth.cleanup(sid)

    def user(self,oid=ALICE):
        sid=auth.new_session();self.sids.append(sid)
        auth._SESSIONS[sid].update(state='authenticated',user={'object_id':oid,'tenant_id':TENANT,'name':'Alice' if oid==ALICE else 'Bob','username':'operator@example.com'},expires=time.time()+600)
        return sid

    def test_configuration_requires_tenant_exact_origin_and_https_for_remote_access(self):
        auth.validate_config()
        for values in ({'MIGRATOR_PUBLIC_ORIGIN':'http://remote.example'}, {'MIGRATOR_PUBLIC_ORIGIN':'https://remote.example/path'},
                       {'MIGRATOR_TENANT_ID':'common'}, {'MIGRATOR_ALLOWED_OBJECT_IDS':'*'}):
            with self.subTest(values=values),patch.dict(os.environ,values),self.assertRaises(ValueError):auth.validate_config()
        with patch.dict(os.environ,{'MIGRATOR_PUBLIC_ORIGIN':'https://migrator.example'}):
            auth.validate_config();self.assertIn('Secure',auth.cookie('opaque'));self.assertIn('HttpOnly',auth.cookie('opaque'));self.assertIn('SameSite=Strict',auth.cookie('opaque'))

    def test_each_command_uses_only_the_callers_private_azure_profile(self):
        alice,bob=self.user(),self.user(BOB)
        a=auth.run_as(alice,auth.command_env);b=auth.run_as(bob,auth.command_env)
        self.assertNotEqual(a['AZURE_CONFIG_DIR'],b['AZURE_CONFIG_DIR'])
        self.assertEqual(a['AZURE_CONFIG_DIR'],auth._SESSIONS[alice]['profile'])
        self.assertNotEqual(a['AZURE_CONFIG_DIR'],os.environ['AZURE_CONFIG_DIR'])
        self.assertEqual(a['KUBECONFIG'],auth._KUBECONFIG_PATH)
        self.assertIsNone(auth.current_session.get())
        with self.assertRaises(auth.AuthError):auth.command_env()

    def test_concurrent_kubectl_calls_keep_user_profiles_out_of_global_environment(self):
        from concurrent.futures import ThreadPoolExecutor
        alice,bob=self.user(),self.user(BOB);barrier=threading.Barrier(2);seen=[]
        def invoke(args,**kwargs):
            seen.append(kwargs['env']['AZURE_CONFIG_DIR']);barrier.wait(timeout=3)
            return types.SimpleNamespace(returncode=0,stdout='{}',stderr='')
        with patch('migrator.kubernetes.shutil.which',return_value='/usr/bin/kubectl'),patch('migrator.kubernetes.subprocess.run',side_effect=invoke):
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(auth.run_as,sid,kubernetes.run,['--context','source','get','pods']) for sid in (alice,bob)]
                for future in futures:self.assertEqual(future.result(),'{}')
        self.assertEqual(set(seen),{auth._SESSIONS[alice]['profile'],auth._SESSIONS[bob]['profile']})
        self.assertEqual(os.environ['AZURE_CONFIG_DIR'],'/SHARED-MUST-NOT-BE-USED')

    def test_histories_are_private_and_other_users_cannot_read_or_update_records(self):
        alice,bob=self.user(),self.user(BOB)
        row=auth.run_as(alice,history.save,{'status':'Applying','name':'source'})
        iam_row=auth.run_as(alice,iam_history.save,{'status':'Applied','role':'Reader'})
        self.assertEqual(len(auth.run_as(alice,history.listing)['operations']),1)
        self.assertEqual(auth.run_as(bob,history.listing)['operations'],[])
        self.assertEqual(auth.run_as(bob,iam_history.listing)['operations'],[])
        self.assertEqual(len(auth.run_as(alice,iam_history.listing)['operations']),1)
        with self.assertRaises(auth.AuthError):auth.run_as(bob,history.get,row['id'])
        with self.assertRaises(auth.AuthError):auth.run_as(bob,history.update,row['id'],{'status':'Applied'})
        self.assertNotEqual(row['owner'],auth.run_as(bob,auth.owner))

    def test_other_users_cannot_consume_or_discard_preview_tokens(self):
        alice,bob=self.user(),self.user(BOB)
        owner=auth.run_as(alice,auth.owner)
        kubernetes._PLANS['owned']={'owner':owner,'expires':time.monotonic()+300}
        iam._PLANS['owned']={'owner':owner,'expires':time.monotonic()+300}
        self.addCleanup(kubernetes._PLANS.pop,'owned',None);self.addCleanup(iam._PLANS.pop,'owned',None)
        for execute in (kubernetes.sync_execute,kubernetes.source_scale_execute,kubernetes.rollback_execute,iam.sync_execute):
            with self.subTest(execute=execute),self.assertRaises(auth.AuthError):auth.run_as(bob,execute,{'token':'owned','confirm':True})
        with self.assertRaises(auth.AuthError):auth.run_as(bob,kubernetes.discard_plans,{'tokens':['owned']})
        with self.assertRaises(auth.AuthError):auth.run_as(bob,iam.discard,{'token':'owned'})
        self.assertIn('owned',kubernetes._PLANS);self.assertIn('owned',iam._PLANS)
        auth.run_as(alice,kubernetes.discard_plans,{'tokens':['owned']});auth.run_as(alice,iam.discard,{'token':'owned'})

    def test_background_jobs_keep_the_submitting_user_and_hide_results_from_other_users(self):
        alice,bob=self.user(),self.user(BOB);event=threading.Event();seen=[]
        def analyze(body):seen.append((auth.owner(),auth.command_env()['AZURE_CONFIG_DIR']));event.set();return {'resources':[]}
        with patch('migrator.jobs.live_analyze',side_effect=analyze):
            job=auth.run_as(alice,jobs.start,{'source_context':'source','target_context':'target','namespaces':['demo']})
            self.addCleanup(jobs._JOBS.pop,job['job_id'],None)
            self.assertTrue(event.wait(3))
            with self.assertRaises(auth.AuthError):auth.run_as(bob,jobs.status,job['job_id'])
        self.assertEqual(seen[0][0],auth.run_as(alice,auth.owner));self.assertEqual(seen[0][1],auth._SESSIONS[alice]['profile'])

    def test_retry_discards_previous_account_cache_before_starting_another_login(self):
        sid=self.user();value=auth._SESSIONS[sid];value['state']='failed'
        profile=Path(value['profile']);profile.mkdir();(profile/'old-account').write_text('PRIVATE-CACHE')
        with patch('migrator.auth.threading.Thread') as worker:auth.start_login(sid)
        self.assertFalse(profile.exists());self.assertNotIn('user',value)
        self.assertEqual(value['state'],'starting');worker.return_value.start.assert_called_once()

    def test_logout_removes_cache_and_prevents_fallback_to_shared_credentials(self):
        alice=self.user();root=auth._SESSIONS[alice]['root'];Path(auth._SESSIONS[alice]['profile']).mkdir()
        auth.cleanup(alice)
        self.assertFalse(Path(root).exists())
        with self.assertRaises(auth.AuthError):auth.run_as(alice,auth.command_env)

    def test_kubeconfig_keeps_contexts_but_discards_admin_certificates_tokens_and_exec_env(self):
        source={'current-context':'source','clusters':[{'name':'cluster','cluster':{'server':'https://api.example','certificate-authority-data':'CA'}}],
                'contexts':[{'name':'source','context':{'cluster':'cluster','user':'admin'}}],
                'users':[{'name':'admin','user':{'token':'PRIVATE-TOKEN','password':'PRIVATE-PASSWORD','client-certificate-data':'PRIVATE-CERT',
                          'client-key-data':'PRIVATE-KEY','exec':{'command':'evil-plugin','env':[{'name':'AZURE_CONFIG_DIR','value':'SHARED'}],
                          'args':['--server-id','spn:audience']}}}]}
        original=copy.deepcopy(source);result=auth.sanitized_kubeconfig(source,TENANT)
        self.assertEqual(source,original)
        self.assertNotIn('PRIVATE',json.dumps(result));self.assertNotIn('SHARED',json.dumps(result));self.assertNotIn('evil-plugin',json.dumps(result))
        self.assertEqual(result['contexts'][0]['name'],'source');self.assertEqual(result['users'][0]['user']['exec']['command'],'kubelogin')
        self.assertIn('audience',result['users'][0]['user']['exec']['args'])

    def test_login_worker_rejects_wrong_tenant_and_never_exposes_token_fields(self):
        sid=auth.new_session();self.sids.append(sid)
        lines=[{'state':'pending','verification_uri':'https://microsoft.com/devicelogin','user_code':'USER-CODE','expires_in':900},
               {'state':'authenticated','user':{'object_id':ALICE,'tenant_id':BOB,'name':'Wrong tenant'}}]
        process=types.SimpleNamespace(stdout=io.StringIO('\n'.join(json.dumps(item) for item in lines)),wait=lambda timeout:0,poll=lambda:0,terminate=lambda:None)
        with patch('migrator.auth.subprocess.Popen',return_value=process) as invoke:auth._login_worker(sid)
        self.assertEqual(auth._SESSIONS[sid]['state'],'failed')
        self.assertIn('not allowed',auth._SESSIONS[sid]['error'])
        self.assertEqual(invoke.call_args.kwargs['env']['AZURE_CONFIG_DIR'],auth._SESSIONS[sid]['profile'])

    def test_http_auth_gate_rejects_anonymous_and_cross_origin_requests(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        os.environ['MIGRATOR_PUBLIC_ORIGIN']='http://127.0.0.1:'+str(server.server_port)
        client=HTTPConnection('127.0.0.1',server.server_port)
        self.addCleanup(client.close)
        with patch('migrator.server.contexts') as contexts:
            client.request('GET','/api/contexts');response=client.getresponse();self.assertEqual(response.status,401);response.read();contexts.assert_not_called()
        client.request('GET','/');response=client.getresponse();self.assertEqual(response.status,302);response.read()
        client.request('GET','/signin');response=client.getresponse();self.assertEqual(response.status,200);cookie=response.getheader('Set-Cookie');self.assertIn('HttpOnly',cookie);self.assertIn(b'Sign in with Microsoft',response.read())
        sid=auth.session_id(types.SimpleNamespace(headers={'Cookie':cookie}));self.sids.append(sid)
        alice=self.user()
        client.request('POST','/api/sync/execute','{}',{'Content-Type':'application/json','Cookie':auth.cookie(alice),'Origin':'https://evil.example'})
        response=client.getresponse();self.assertEqual(response.status,403);response.read()
        client.request('GET','/auth/status',headers={'Cookie':auth.cookie(alice)})
        response=client.getresponse();data=json.loads(response.read());self.assertEqual(data['user']['object_id'],ALICE)
        self.assertNotIn('profile',data);self.assertNotIn('root',data);self.assertNotIn('process',data)


class DeviceHelperTests(unittest.TestCase):
    def test_helper_emits_only_challenge_and_identity_not_access_refresh_or_device_tokens(self):
        from container import device_login
        class PublicClient:
            def initiate_device_flow(self,*args,**kwargs):return {'user_code':'PUBLIC-CODE','verification_uri':'https://microsoft.com/devicelogin','device_code':'PRIVATE-DEVICE','expires_in':900}
            def acquire_token_by_device_flow(self,*args,**kwargs):return {'access_token':'PRIVATE-ACCESS','refresh_token':'PRIVATE-REFRESH',
                'id_token_claims':{'oid':ALICE,'tid':TENANT,'name':'Alice','preferred_username':'alice@example.com'}}
        class CLI:
            def invoke(self,args):
                self.args=args;app=PublicClient();flow=app.initiate_device_flow();app.acquire_token_by_device_flow(flow)
                print('PRIVATE-OUTPUT');print('PRIVATE-STDERR',file=__import__('sys').stderr);return 0
        modules={'msal':types.SimpleNamespace(PublicClientApplication=PublicClient),
                 'azure':types.ModuleType('azure'),'azure.cli':types.ModuleType('azure.cli'),
                 'azure.cli.core':types.SimpleNamespace(get_default_cli=lambda:CLI())}
        stdout=io.StringIO();stderr=io.StringIO()
        with patch.dict('sys.modules',modules),patch.dict(os.environ,{'MIGRATOR_TENANT_ID':TENANT}),patch('sys.stdout',stdout),patch('sys.stderr',stderr):
            self.assertEqual(device_login.main(),0)
        messages=[json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(messages[0]['user_code'],'PUBLIC-CODE');self.assertEqual(messages[1]['user']['object_id'],ALICE)
        self.assertNotIn('PRIVATE',stdout.getvalue()+stderr.getvalue())
