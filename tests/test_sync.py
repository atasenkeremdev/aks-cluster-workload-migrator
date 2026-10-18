import json
import time
import unittest
import tempfile
import os
from unittest.mock import patch
from migrator import kubernetes as k
from migrator.core import analyze


def obj(kind='ConfigMap', version=None):
    value={'apiVersion':'v1','kind':kind,'metadata':{'name':'app','namespace':'demo'},'data':{'key':'PRIVATE'}}
    if version:
        value['metadata']['resourceVersion']=version
    return value


class SyncTests(unittest.TestCase):
    def setUp(self):
        k._PLANS.clear()
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        env=patch.dict(os.environ,{'MIGRATOR_STATE_DIR':temp.name});env.start();self.addCleanup(env.stop)
        self.body={'source_context':'src','target_context':'dst','namespace':'demo','name':'app','kind':'ConfigMap','apiVersion':'v1'}

    @patch('migrator.kubernetes.context_args', side_effect=lambda c:['--context', c])
    @patch('migrator.kubernetes.run')
    def test_create_only_after_confirmation(self, run, _):
        run.side_effect=[json.dumps(obj()), '', '{}']
        plan=k.sync_plan(self.body)
        self.assertEqual(plan['operation'],'create')
        self.assertIn('--dry-run=server',run.call_args.args[0])
        with self.assertRaises(ValueError):
            k.sync_execute({'token':plan['token']})
        applied=obj();applied['metadata'].update(uid='uid-1',resourceVersion='2')
        run.side_effect=['',json.dumps(applied)]
        self.assertEqual(k.sync_execute({'token':plan['token'],'confirm':True})['status'],'Applied')
        self.assertIn('create',run.call_args.args[0])
        self.assertNotIn('--dry-run=server',run.call_args.args[0])
        with self.assertRaises(ValueError):
            k.sync_execute({'token':plan['token'],'confirm':True})

    @patch('migrator.kubernetes.context_args', side_effect=lambda c:['--context', c])
    @patch('migrator.kubernetes.run')
    def test_target_changed_aborts(self, run, _):
        run.side_effect=[json.dumps(obj()),json.dumps(obj(version='1')),'{}']
        plan=k.sync_plan(self.body)
        self.assertEqual(run.call_args.args[1]['metadata']['resourceVersion'],'1')
        run.side_effect=[json.dumps(obj(version='2'))]
        with self.assertRaises(k.ClusterError) as error:
            k.sync_execute({'token':plan['token'],'confirm':True})
        self.assertEqual(error.exception.status,409)
        self.assertEqual(run.call_count,4)

    @patch('migrator.kubernetes.context_args', side_effect=lambda c:['--context', c])
    @patch('migrator.kubernetes.run')
    def test_failed_dry_run_has_no_plan(self, run, _):
        run.side_effect=[json.dumps(obj()),'',k.ClusterError('Forbidden')]
        with self.assertRaises(k.ClusterError):
            k.sync_plan(self.body)
        self.assertFalse(k._PLANS)

    @patch('migrator.kubernetes.context_args', side_effect=lambda c:['--context', c])
    @patch('migrator.kubernetes.run')
    def test_secret_preview_redacted(self, run, _):
        run.side_effect=[json.dumps(obj('Secret')),'','{}']
        plan=k.sync_plan({**self.body,'kind':'Secret'})
        self.assertNotIn('PRIVATE',json.dumps(plan))

    def test_diff_exposes_both_sides_and_secret_paths(self):
        source=obj('Secret'); target=obj('Secret'); target['data']['key']='OTHER'
        report=analyze([source],[target],['demo'])
        row=report['resources'][0]
        self.assertIn('/data/key',row['diff_paths'])
        self.assertEqual(row['target_manifest']['data']['key'],'[REDACTED]')
        self.assertNotIn('PRIVATE',json.dumps(report))
        self.assertNotIn('OTHER',json.dumps(report))

    def test_expired_plan(self):
        k._PLANS['expired']={'expires':time.monotonic()-1}
        with self.assertRaises(ValueError):
            k.sync_execute({'token':'expired','confirm':True})

    def test_protected_scope_never_reads(self):
        with patch('migrator.kubernetes.run') as run:
            with self.assertRaises(ValueError):
                k.sync_plan({**self.body,'namespace':'kube-system'})
            run.assert_not_called()
