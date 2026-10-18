import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from migrator import kubernetes as k
from migrator import history


def resource(version='1', value='old', **status):
    return {'apiVersion':'v1','kind':'ConfigMap','metadata':{'name':'app','namespace':'demo','uid':'target-uid','resourceVersion':version},'data':{'key':value},'status':status}


class UpdateStrategyTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        env=patch.dict(os.environ,{'MIGRATOR_STATE_DIR':directory.name});env.start();self.addCleanup(env.stop)
        contexts=patch('migrator.kubernetes.context_args',side_effect=lambda context:['--context',context]);contexts.start();self.addCleanup(contexts.stop)
        k._PLANS.clear()
        self.body={'source_context':'source','target_context':'target','namespace':'demo','kind':'ConfigMap','name':'app','apiVersion':'v1','mode':'activate'}

    @patch('migrator.kubernetes.run')
    def test_guarded_merge_dryrun_and_confirmed_write(self, run):
        run.side_effect=[json.dumps(resource(value='new')),json.dumps(resource()),'{}']
        plan=k.sync_plan({**self.body,'strategy':'merge'})
        command,manifest=run.call_args.args
        self.assertIn('patch',command)
        self.assertIn('--patch-file=-',command)
        self.assertIn('--dry-run=server',command)
        self.assertEqual(manifest['metadata']['resourceVersion'],'1')
        self.assertNotIn('--force-conflicts',command)
        run.side_effect=[json.dumps(resource(version='2', ready=True)),json.dumps(resource(version='3',value='new'))]
        result=k.sync_execute({'token':plan['token'],'confirm':True})
        self.assertEqual(result['status'],'Applied')
        command,manifest=run.call_args.args
        self.assertEqual(manifest['metadata']['resourceVersion'],'2')
        self.assertNotIn('--dry-run=server',command)
        self.assertEqual(history.get(result['operation_id'])['strategy'],'merge')

    @patch('migrator.kubernetes.run')
    def test_external_config_edit_blocks_even_guarded_merge(self, run):
        run.side_effect=[json.dumps(resource(value='new')),json.dumps(resource()),'{}']
        plan=k.sync_plan({**self.body,'strategy':'merge'})
        run.side_effect=[json.dumps(resource(version='2',value='someone-else'))]
        with self.assertRaisesRegex(k.ClusterError,'configuration changed'):
            k.sync_execute({'token':plan['token'],'confirm':True})
        self.assertEqual(run.call_count,4)

    @patch('migrator.kubernetes.run')
    def test_recreated_target_blocks_write(self, run):
        run.side_effect=[json.dumps(resource(value='new')),json.dumps(resource()),'{}']
        plan=k.sync_plan(self.body)
        recreated=resource();recreated['metadata']['uid']='different-uid'
        run.side_effect=[json.dumps(recreated)]
        with self.assertRaises(k.ClusterError):
            k.sync_execute({'token':plan['token'],'confirm':True})

    @patch('migrator.kubernetes.run')
    def test_known_conflict_is_not_unknown_write(self, run):
        run.side_effect=[json.dumps(resource(value='new')),json.dumps(resource()),'{}']
        plan=k.sync_plan(self.body)
        run.side_effect=[json.dumps(resource()),k.ClusterError('Resource version changed',409,'resource_version')]
        with self.assertRaises(k.ClusterError) as error:
            k.sync_execute({'token':plan['token'],'confirm':True})
        self.assertEqual(error.exception.category,'resource_version')
        self.assertEqual(history.listing()['operations'][0]['status'],'Conflict')

    @patch('migrator.kubernetes.shutil.which',return_value='/bin/kubectl')
    @patch('migrator.kubernetes.subprocess.run')
    def test_ownership_diagnostic_keeps_only_manager_metadata(self, run, _):
        run.return_value=subprocess.CompletedProcess([],1,'','Apply failed with 1 conflict: conflict with "terraform-provider-kubernetes" using apps/v1: .spec.replicas\nPRIVATE_TOKEN_VALUE')
        with self.assertRaises(k.ClusterError) as error:
            k.run(['apply','--server-side'])
        self.assertEqual(error.exception.category,'field_ownership')
        self.assertIn('terraform-provider-kubernetes',str(error.exception))
        self.assertNotIn('PRIVATE_TOKEN_VALUE',str(error.exception))

    @patch('migrator.kubernetes.shutil.which',return_value='/bin/kubectl')
    @patch('migrator.kubernetes.subprocess.run')
    def test_resource_version_diagnostic_is_distinct(self, run, _):
        run.return_value=subprocess.CompletedProcess([],1,'','Error from server (Conflict): the object has been modified')
        with self.assertRaises(k.ClusterError) as error:
            k.run(['patch','ConfigMap','app'])
        self.assertEqual(error.exception.category,'resource_version')

    @patch('migrator.kubernetes.shutil.which',return_value='/bin/kubectl')
    @patch('migrator.kubernetes.subprocess.run')
    def test_patch_transport_uses_private_temporary_file_then_removes_it(self, run, _):
        paths=[]
        def invoke(command, **options):
            filename=next(arg.split('=',1)[1] for arg in command if arg.startswith('--patch-file='))
            paths.append(filename)
            with open(filename) as stream:
                self.assertEqual(json.load(stream),{'data':{'password':'PRIVATE_VALUE'}})
            self.assertNotIn('PRIVATE_VALUE',' '.join(command))
            self.assertNotIn('input',options)
            if os.name!='nt':
                self.assertEqual(os.stat(filename).st_mode & 0o777,0o600)
            return subprocess.CompletedProcess(command,0,'{}','')
        run.side_effect=invoke
        k.run(['patch','Secret','app','--patch-file=-'],{'data':{'password':'PRIVATE_VALUE'}})
        self.assertFalse(os.path.exists(paths[0]))

    @patch('migrator.kubernetes.run')
    def test_preview_retries_once_for_controller_only_version_change(self, run):
        run.side_effect=[json.dumps(resource(value='new')),json.dumps(resource()),
                         k.ClusterError('version conflict',409,'resource_version'),
                         json.dumps(resource(version='2', ready=True)),'{}']
        plan=k.sync_plan(self.body)
        self.assertEqual(plan['manifest']['metadata']['resourceVersion'],'2')
        self.assertEqual(run.call_count,5)

    @patch('migrator.kubernetes.run')
    def test_preview_never_retries_over_configuration_edits(self, run):
        run.side_effect=[json.dumps(resource(value='new')),json.dumps(resource()),
                         k.ClusterError('version conflict',409,'resource_version'),
                         json.dumps(resource(version='2',value='external-edit'))]
        with self.assertRaisesRegex(k.ClusterError,'configuration changed during preview'):
            k.sync_plan(self.body)
        self.assertEqual(run.call_count,4)


    @patch('migrator.kubernetes.run')
    def test_configuration_sync_has_its_own_action(self, run):
        run.side_effect=[json.dumps(resource(value='new')),json.dumps(resource()),'{}']
        plan=k.sync_plan({**self.body,'mode':'sync'})
        self.assertEqual(plan['mode'],'sync')
        self.assertIn('Configuration changes',plan['warnings'][0])

    @patch('migrator.kubernetes.run')
    def test_generic_sync_cannot_implicitly_activate_workload(self, run):
        workload=resource();workload['kind']='Deployment';workload['apiVersion']='apps/v1'
        run.side_effect=[json.dumps(workload),'']
        with self.assertRaisesRegex(ValueError,'explicit prepare or activate'):
            k.sync_plan({**self.body,'kind':'Deployment','apiVersion':'apps/v1','mode':'sync'})
