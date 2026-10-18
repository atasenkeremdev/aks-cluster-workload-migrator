import copy
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from migrator import history
from migrator import kubernetes as k
from migrator.readiness import evaluate
from migrator.dependencies import references


def workload(replicas=2, version='1'):
    return {'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':'app','namespace':'demo','uid':'u1','resourceVersion':version,'generation':1},'spec':{'replicas':replicas,'template':{'spec':{'containers':[{'name':'app','image':'registry/app:v1'}]}}}}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        env=patch.dict(os.environ,{'MIGRATOR_STATE_DIR':temp.name});env.start();self.addCleanup(env.stop)
        self.context=patch('migrator.kubernetes.context_args',side_effect=lambda c:['--context',c]);self.context.start();self.addCleanup(self.context.stop)
        k._PLANS.clear()
        self.body={'source_context':'src','target_context':'dst','namespace':'demo','name':'app','kind':'Deployment','apiVersion':'apps/v1'}

    @patch('migrator.kubernetes.run')
    def test_prepare_zero_replicas_and_hpa_check(self, run):
        run.side_effect=[json.dumps(workload()),'',json.dumps({'items':[]}),'{}']
        plan=k.sync_plan(self.body)
        self.assertEqual(plan['mode'],'prepare')
        self.assertEqual(plan['manifest']['spec']['replicas'],0)
        self.assertIn('horizontalpodautoscalers',run.call_args_list[2].args[0])

    @patch('migrator.kubernetes.run')
    def test_hpa_blocks_passive(self, run):
        run.side_effect=[json.dumps(workload()),'',json.dumps({'items':[{'spec':{'scaleTargetRef':{'kind':'Deployment','name':'app'}}}]})]
        with self.assertRaisesRegex(ValueError,'HPA'):
            k.sync_plan(self.body)
        self.assertFalse(k._PLANS)

    @patch('migrator.kubernetes.run')
    def test_activation_preserves_source_replicas(self, run):
        run.side_effect=[json.dumps(workload()),'','{}']
        self.assertEqual(k.sync_plan({**self.body,'mode':'activate'})['manifest']['spec']['replicas'],2)

    @patch('migrator.kubernetes.run')
    def test_cronjob_prepared_suspended(self, run):
        obj={'apiVersion':'batch/v1','kind':'CronJob','metadata':{'name':'app','namespace':'demo'},'spec':{'schedule':'* * * * *'}}
        run.side_effect=[json.dumps(obj),'','{}']
        plan=k.sync_plan({**self.body,'kind':'CronJob','apiVersion':'batch/v1'})
        self.assertTrue(plan['manifest']['spec']['suspend'])

    @patch('migrator.kubernetes.run')
    def test_target_controller_resource_cannot_be_overwritten(self, run):
        obj=workload();target=workload();target['metadata']['ownerReferences']=[{'controller':True,'kind':'Operator'}]
        run.side_effect=[json.dumps(obj),json.dumps(target)]
        with self.assertRaisesRegex(ValueError,'Target resource'):
            k.sync_plan(self.body)

    @patch('migrator.kubernetes.run')
    def test_missing_dependency_stops_before_dry_run(self, run):
        obj=workload();obj['spec']['template']['spec']['serviceAccountName']='missing'
        run.side_effect=[json.dumps(obj),'','']
        with self.assertRaisesRegex(ValueError,'dependency missing'):
            k.sync_plan({**self.body,'mode':'activate'})
        self.assertFalse(k._PLANS)

    @patch('migrator.kubernetes.run')
    def test_durable_snapshot_restore_and_conflict(self, run):
        before=workload(1);source=workload(3);applied=workload(3,'2')
        run.side_effect=[json.dumps(source),json.dumps(before),'{}']
        token=k.sync_plan({**self.body,'mode':'activate'})['token']
        run.side_effect=[json.dumps(before),json.dumps(applied)]
        operation=k.sync_execute({'token':token,'confirm':True})
        record=history.get(operation['operation_id'])
        self.assertEqual(record['before']['spec']['replicas'],1)
        self.assertNotIn('before',history.listing()['operations'][0])
        run.side_effect=[json.dumps(applied),'{}']
        preview=k.rollback_plan({'operation_id':record['id']})
        self.assertEqual(preview['manifest']['spec']['replicas'],1)
        run.side_effect=[json.dumps(applied),'{}']
        result=k.rollback_execute({'token':preview['token'],'confirm':True})
        self.assertEqual(result['status'],'RollbackApplied')
        self.assertFalse(history.get(record['id'])['rollback_available'])

    @patch('migrator.kubernetes.run')
    def test_rollback_rejects_later_configuration_edit(self, run):
        current=workload();record=history.save({'target':'dst','namespace':'demo','name':'app','resource':'Deployment.apps','kind':'Deployment','mode':'activate','before':workload(1),'applied_uid':'u1','applied_version':'1','applied_fingerprint':k.fingerprint(current),'rollback_available':True})
        current['spec']['replicas']=9
        run.return_value=json.dumps(current)
        with self.assertRaisesRegex(k.ClusterError,'configuration changed'):
            k.rollback_plan({'operation_id':record['id']})

    @patch('migrator.kubernetes.run')
    def test_unknown_write_preserves_journal(self, run):
        run.side_effect=[json.dumps(workload()),'','{}']
        token=k.sync_plan({**self.body,'mode':'activate'})['token']
        run.side_effect=['',k.ClusterError('timeout')]
        with self.assertRaisesRegex(k.ClusterError,'uncertain'):
            k.sync_execute({'token':token,'confirm':True})
        self.assertEqual(history.listing()['operations'][0]['status'],'Unknown')

    def test_readiness_is_not_apply_success(self):
        obj=workload();self.assertEqual(evaluate(obj,'activate')['state'],'RollingOut')
        obj['status']={'observedGeneration':1,'replicas':2,'readyReplicas':2,'updatedReplicas':2,'availableReplicas':2}
        self.assertEqual(evaluate(obj,'activate')['state'],'Ready')
        obj['status']['conditions']=[{'type':'Progressing','status':'False'}]
        self.assertEqual(evaluate(obj,'activate')['state'],'Failed')

    def test_passive_readiness_waits_for_shutdown(self):
        obj=workload(0);obj['status']={'observedGeneration':1,'replicas':1}
        self.assertEqual(evaluate(obj,'prepare')['state'],'RollingOut')
        obj['status']['replicas']=0
        self.assertEqual(evaluate(obj,'prepare')['state'],'Prepared')

    def test_reference_extraction(self):
        obj=workload();pod=obj['spec']['template']['spec']
        pod['containers'][0]['env']=[{'valueFrom':{'secretKeyRef':{'name':'auth','key':'password'}}}]
        pod['imagePullSecrets']=[{'name':'registry'}]
        pod['volumes']=[{'persistentVolumeClaim':{'claimName':'data'}}]
        self.assertEqual(references(obj),[('persistentvolumeclaim','data'),('secret','auth'),('secret','registry')])

class JournalAndRollbackTests(unittest.TestCase):
    setUp = WorkflowTests.setUp
    @patch('migrator.kubernetes.run')
    def test_secret_backup_never_persisted(self, run):
        source={'apiVersion':'v1','kind':'Secret','metadata':{'name':'app','namespace':'demo'},'data':{'password':'TOP_SECRET'}}
        before=copy.deepcopy(source);before['metadata'].update(uid='u1',resourceVersion='1')
        applied=copy.deepcopy(before);applied['metadata']['resourceVersion']='2'
        run.side_effect=[json.dumps(source),json.dumps(before),'{}']
        token=k.sync_plan({**self.body,'kind':'Secret','apiVersion':'v1'})['token']
        run.side_effect=[json.dumps(before),json.dumps(applied)]
        result=k.sync_execute({'token':token,'confirm':True})
        record=history.get(result['operation_id'])
        self.assertFalse(record['rollback_available'])
        self.assertIsNone(record['before'])
        self.assertNotIn('TOP_SECRET',history.path().read_bytes().decode('latin1'))

    @patch('migrator.kubernetes.run')
    def test_created_resource_rollback_uses_delete_preconditions(self, run):
        current=workload(2,'4')
        record=history.save({'target':'dst','namespace':'demo','name':'app','kind':'Deployment','resource':'Deployment.apps','mode':'activate','before':None,'applied_uid':'u1','applied_fingerprint':k.fingerprint(current),'rollback_available':True})
        run.return_value=json.dumps(current)
        preview=k.rollback_plan({'operation_id':record['id']})
        discovery={'resources':[{'name':'deployments','kind':'Deployment','namespaced':True}]}
        run.side_effect=[json.dumps(current),json.dumps(discovery),'{}']
        result=k.rollback_execute({'token':preview['token'],'confirm':True})
        self.assertEqual(result['status'],'DeletionRequested')
        command,payload=run.call_args.args
        self.assertIn('/apis/apps/v1/namespaces/demo/deployments/app',command)
        self.assertEqual(payload['preconditions'],{'uid':'u1','resourceVersion':'4'})

    @patch('migrator.kubernetes.run')
    def test_rollback_preview_conflict_rejected(self, run):
        current=workload();record=history.save({'target':'dst','namespace':'demo','name':'app','kind':'Deployment','resource':'Deployment.apps','mode':'activate','before':workload(1),'applied_uid':'u1','applied_fingerprint':k.fingerprint(current),'rollback_available':True})
        run.side_effect=[json.dumps(current),'{}']
        token=k.rollback_plan({'operation_id':record['id']})['token']
        current['metadata']['resourceVersion']='9'
        run.side_effect=[json.dumps(current)]
        with self.assertRaisesRegex(k.ClusterError,'changed after rollback preview'):
            k.rollback_execute({'token':token,'confirm':True})
        self.assertNotEqual(history.get(record['id']).get('status'),'RollbackApplied')

    def test_health_update_cannot_resurrect_rollback(self):
        record=history.save({'status':'Applied','rollback_available':True})
        history.update(record['id'],{'status':'RollingBack','rollback_available':False},require_rollback=True)
        history.update(record['id'],{'health':{'state':'Ready'}})
        actual=history.get(record['id'])
        self.assertEqual(actual['status'],'RollingBack')
        self.assertFalse(actual['rollback_available'])
        with self.assertRaises(ValueError):
            history.update(record['id'],{'status':'RollingBack'},require_rollback=True)

    @patch('migrator.kubernetes.run')
    def test_health_detects_configuration_drift(self, run):
        current=workload();record=history.save({'target':'dst','namespace':'demo','name':'app','resource':'Deployment.apps','mode':'activate','status':'Applied','applied_uid':'u1','applied_fingerprint':k.fingerprint(current)})
        current['spec']['replicas']=8
        run.return_value=json.dumps(current)
        self.assertEqual(k.operation_health({'operation_id':record['id']})['state'],'Drifted')

    @patch('migrator.kubernetes.run',return_value='')
    def test_deletion_completion_is_verified(self, run):
        record=history.save({'target':'dst','namespace':'demo','name':'app','resource':'Deployment.apps','mode':'activate','status':'DeletionRequested','applied_uid':'u1'})
        self.assertEqual(k.operation_health({'operation_id':record['id']})['state'],'Deleted')
        self.assertIn('--ignore-not-found',run.call_args.args[0])

    def test_deployment_not_ready_before_available(self):
        current=workload()
        current['status']={'observedGeneration':1,'replicas':2,'readyReplicas':2,'updatedReplicas':2,'availableReplicas':1}
        self.assertEqual(evaluate(current,'activate')['state'],'RollingOut')

    @patch('migrator.kubernetes.run')
    def test_passive_unsupported_controller_kind_is_blocked(self, run):
        current=workload();current['kind']='DaemonSet'
        run.side_effect=[json.dumps(current),'']
        with self.assertRaisesRegex(ValueError,'Passive preparation is unsupported'):
            k.sync_plan({**self.body,'kind':'DaemonSet'})
        self.assertFalse(k._PLANS)

    def test_readiness_result_from_before_rollback_is_rejected(self):
        record=history.save({'status':'Applied','rollback_available':True,'health':{'state':'Ready'}})
        history.update(record['id'],{'status':'RollingBack','health':None,'rollback_available':False},require_rollback=True)
        with self.assertRaises(ValueError):
            history.update(record['id'],{'health':{'state':'Ready'}},expected_status='Applied')
        self.assertIsNone(history.get(record['id'])['health'])
