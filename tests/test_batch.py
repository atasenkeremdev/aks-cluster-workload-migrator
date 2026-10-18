import json
import unittest
from unittest.mock import patch
from migrator import kubernetes as k


class BatchTests(unittest.TestCase):
    @patch('migrator.kubernetes.context_args',return_value=['--context','src'])
    @patch('migrator.kubernetes.run')
    def test_all_supported_kinds_share_one_process_per_namespace(self, run, _):
        kinds=sorted(k.KINDS-{'Namespace'})
        ns={'apiVersion':'v1','kind':'Namespace','metadata':{'name':'demo'}}
        objects=[{'apiVersion':'v1','kind':kind,'metadata':{'name':'app','namespace':'demo'}} for kind in kinds]
        discovery='\n'.join(f'{kind.lower()} v1 true {kind} get,list' for kind in kinds)
        run.side_effect=[json.dumps({'items':[ns]}),discovery,json.dumps({'items':objects})]
        result,missing=k.snapshot('src',['demo'])
        self.assertEqual(result,[ns,*objects])
        self.assertEqual(missing,[])
        self.assertEqual(run.call_count,3)  # namespace list, discovery, one batch
        self.assertEqual(run.call_args.args[0][5],','.join(kinds))

    @patch('migrator.kubernetes.context_args',return_value=['--context','src'])
    @patch('migrator.kubernetes.run')
    def test_partial_batch_failure_aborts_analysis(self, run, _):
        run.side_effect=[json.dumps({'items':[{'metadata':{'name':'demo'}}]}),
                         'deployments apps/v1 true Deployment list\nsecrets v1 true Secret list',k.ClusterError('Forbidden')]
        with self.assertRaises(k.ClusterError):
            k.snapshot('src',['demo'])

    @patch('migrator.kubernetes.shutil.which',return_value='/bin/kubectl')
    @patch('migrator.kubernetes.subprocess.run')
    def test_batch_keeps_api_timeout_and_scales_process_budget(self, run, _):
        run.return_value.returncode=0;run.return_value.stdout='{}'
        k.run(['get','Deployment,Secret','-o','json'])
        self.assertEqual(run.call_args.kwargs['timeout'],50)
        self.assertIn('--request-timeout=15s',run.call_args.args[0])

    def test_multiselect_namespace_limit(self):
        k.validate_selection('source','target',[f'app-{i}' for i in range(200)])
        with self.assertRaises(ValueError):
            k.validate_selection('source','target',[f'app-{i}' for i in range(201)])
