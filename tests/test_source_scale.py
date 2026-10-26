"""Source-only scale previews, guarded execution and termination checks."""
import copy
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from migrator import kubernetes as k, history
from migrator.readiness import evaluate


def workload(kind='Deployment', replicas=3):
    return {'apiVersion':'apps/v1', 'kind':kind,
            'metadata':{'name':'worker','namespace':'demo','uid':'source-uid','resourceVersion':'10','generation':1},
            'spec':{'replicas':replicas, 'template':{'spec':{'containers':[{'name':'worker','image':'example/worker:1'}]}}},
            'status':{'observedGeneration':1,'replicas':replicas,'readyReplicas':replicas}}


class SourceScaleTests(unittest.TestCase):
    def setUp(self):
        k._PLANS.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(os.environ, {'MIGRATOR_STATE_DIR':self.temp.name})
        self.env.start(); self.addCleanup(self.env.stop)
        contexts = patch('migrator.kubernetes.context_args', side_effect=lambda context:['--context',context])
        contexts.start(); self.addCleanup(contexts.stop)
        self.body = {'source_context':'source','target_context':'target','namespace':'demo',
                     'name':'worker','kind':'Deployment','apiVersion':'apps/v1'}
        self.obj = workload()
        self.commands = []
        self.hpas = []
        self.fail_write = None
        self.on_write = None
        runner = patch('migrator.kubernetes.run', side_effect=self.run_command)
        runner.start(); self.addCleanup(runner.stop)

    def run_command(self, args, payload=None):
        self.commands.append((args, copy.deepcopy(payload)))
        self.assertEqual(args[args.index('--context')+1], 'source', 'Target must never be contacted')
        if 'get' in args:
            return json.dumps({'items':self.hpas} if 'horizontalpodautoscalers' in args else self.obj)
        self.assertIn('--subresource=scale', args)
        if '--dry-run=server' not in args:
            if self.on_write:
                self.on_write()
            if self.fail_write:
                raise self.fail_write
            self.obj['spec']['replicas'] = 0
            self.obj['metadata']['resourceVersion'] = '12'
            self.obj['metadata']['generation'] = 2
        return json.dumps({'apiVersion':'autoscaling/v1','kind':'Scale',
                           'metadata':self.obj['metadata'], 'spec':{'replicas':0}})

    def writes(self):
        return [(args,payload) for args,payload in self.commands if 'patch' in args and '--dry-run=server' not in args]

    def execute(self, token):
        return k.source_scale_execute({'token':token,'confirm':True})

    def test_confirmed_write_only_changes_source_scale_and_records_previous_count(self):
        plan = k.source_scale_plan(self.body)
        self.assertEqual(plan['previous_replicas'], 3)
        self.assertEqual(self.writes(), [])
        with self.assertRaises(ValueError):
            k.source_scale_execute({'token':plan['token']})
        self.on_write = lambda: self.assertEqual(history.listing()['operations'][0]['status'], 'Applying')
        result = self.execute(plan['token'])
        self.assertEqual(result['status'], 'Applied')
        self.assertEqual(len(self.writes()), 1)
        payload = self.writes()[0][1]
        self.assertEqual(payload, {'metadata':{'uid':'source-uid','resourceVersion':'10'},'spec':{'replicas':0}})
        record = history.get(result['operation_id'])
        self.assertEqual(record['cluster_role'], 'source')
        self.assertEqual(record['previous_replicas'], 3)
        self.assertFalse(record['rollback_available'])
        self.assertNotIn('before', record)
        with self.assertRaises(ValueError):
            self.execute(plan['token'])
        self.assertEqual(len(self.writes()), 1)

    def test_statefulset_is_supported(self):
        self.obj = workload('StatefulSet')
        plan = k.source_scale_plan({**self.body,'kind':'StatefulSet'})
        self.execute(plan['token'])
        self.assertIn('StatefulSet.apps', self.writes()[0][0])

    def test_unsupported_kinds_and_exclusions_fail_before_cluster_access(self):
        for overrides in [{'kind':'DaemonSet'}, {'kind':'CronJob'}, {'kind':'ConfigMap'},
                          {'apiVersion':'extensions/v1beta1'}, {'namespace':'kube-system'},
                          {'source_context':'target'}, {'name':'bad/name'}]:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                k.source_scale_plan({**self.body,**overrides})
        self.assertEqual(self.commands, [])

    def test_hpa_blocks_preview_and_new_hpa_blocks_execution(self):
        hpa = {'spec':{'scaleTargetRef':{'kind':'Deployment','name':'worker'}}}
        self.hpas = [hpa]
        with self.assertRaisesRegex(ValueError, 'HPA'):
            k.source_scale_plan(self.body)
        self.assertFalse(k._PLANS)
        self.hpas = []
        plan = k.source_scale_plan(self.body)
        self.hpas = [hpa]
        with self.assertRaisesRegex(ValueError, 'HPA'):
            self.execute(plan['token'])
        self.assertEqual(self.writes(), [])

    def test_changed_or_replaced_source_blocks_execution(self):
        for change in ['uid', 'replicas', 'image']:
            with self.subTest(change=change):
                self.obj = workload()
                plan = k.source_scale_plan(self.body)
                if change == 'uid':
                    self.obj['metadata']['uid'] = 'replacement'
                elif change == 'replicas':
                    self.obj['spec']['replicas'] = 5
                else:
                    self.obj['spec']['template']['spec']['containers'][0]['image'] = 'new'
                with self.assertRaises(k.ClusterError) as error:
                    self.execute(plan['token'])
                self.assertEqual(error.exception.category, 'resource_version')
        self.assertEqual(self.writes(), [])

    def test_status_only_change_refreshes_atomic_version_guard(self):
        plan = k.source_scale_plan(self.body)
        self.obj['metadata']['resourceVersion'] = '11'
        self.obj['status']['readyReplicas'] = 2
        self.execute(plan['token'])
        self.assertEqual(self.writes()[0][1]['metadata']['resourceVersion'], '11')

    def test_controller_owned_or_already_zero_resources_are_blocked(self):
        self.obj['metadata']['ownerReferences'] = [{'controller':True}]
        with self.assertRaises(ValueError):
            k.source_scale_plan(self.body)
        self.obj = workload(replicas=0)
        with self.assertRaisesRegex(ValueError, 'already'):
            k.source_scale_plan(self.body)
        self.assertEqual(self.writes(), [])

    def test_expired_cancelled_and_wrong_action_tokens_cannot_write(self):
        token = k.source_scale_plan(self.body)['token']
        k._PLANS[token]['expires'] = time.monotonic()-1
        with self.assertRaises(ValueError):
            self.execute(token)
        token = k.source_scale_plan(self.body)['token']
        k.discard_plans({'tokens':[token]})
        with self.assertRaises(ValueError):
            self.execute(token)
        token = k.source_scale_plan(self.body)['token']
        with self.assertRaises(ValueError):
            k.sync_execute({'token':token,'confirm':True})
        k._PLANS['sync-token'] = {'expires':time.monotonic()+300}
        with self.assertRaises(ValueError):
            self.execute('sync-token')
        self.assertEqual(self.writes(), [])

    def test_dry_run_failure_does_not_issue_a_token(self):
        with patch('migrator.kubernetes.run', side_effect=k.ClusterError('RBAC Forbidden')):
            with self.assertRaises(k.ClusterError):
                k.source_scale_plan(self.body)
        self.assertFalse(k._PLANS)

    def test_uncertain_and_conflicting_write_outcomes_are_journaled(self):
        for category, status in [(None,'Unknown'), ('resource_version','Conflict')]:
            with self.subTest(category=category):
                self.obj = workload()
                token = k.source_scale_plan(self.body)['token']
                self.fail_write = k.ClusterError('Request failed', category=category)
                with self.assertRaises(k.ClusterError):
                    self.execute(token)
                record = history.listing()['operations'][0]
                self.assertEqual(record['status'], status)
                self.assertFalse(record['rollback_available'])
                self.fail_write = None

    def test_journal_failure_prevents_write(self):
        token = k.source_scale_plan(self.body)['token']
        with patch('migrator.kubernetes.history.save', side_effect=OSError('Disk full')):
            with self.assertRaises(OSError):
                self.execute(token)
        self.assertEqual(self.writes(), [])

    def test_health_waits_for_termination_and_detects_reactivation(self):
        result = self.execute(k.source_scale_plan(self.body)['token'])
        self.assertEqual(k.operation_health({'operation_id':result['operation_id']})['state'], 'RollingOut')
        self.obj['status'] = {'observedGeneration':2, 'replicas':0}
        self.assertEqual(k.operation_health({'operation_id':result['operation_id']})['state'], 'ScaledDown')
        self.obj['spec']['replicas'] = 1
        self.assertEqual(k.operation_health({'operation_id':result['operation_id']})['state'], 'Drifted')
