import json
import subprocess
import unittest
from unittest.mock import patch
from migrator import kubernetes as k


class ClusterTests(unittest.TestCase):
    @patch('migrator.kubernetes.shutil.which', return_value=None)
    def test_missing_kubectl(self, _):
        with self.assertRaises(k.ClusterError) as error:
            k.contexts()
        self.assertEqual(error.exception.status, 503)

    @patch('migrator.kubernetes.shutil.which', return_value='/bin/kubectl')
    @patch('migrator.kubernetes.subprocess.run')
    def test_timeout(self, run, _):
        run.side_effect = subprocess.TimeoutExpired('kubectl', 25)
        with self.assertRaises(k.ClusterError):
            k.contexts()

    @patch('migrator.kubernetes.shutil.which', return_value='/bin/kubectl')
    @patch('migrator.kubernetes.subprocess.run')
    def test_errors_redacted(self, run, _):
        run.return_value = subprocess.CompletedProcess([], 1, '', 'Forbidden private-token-value')
        with self.assertRaises(k.ClusterError) as error:
            k.contexts()
        self.assertIn('Forbidden', str(error.exception))
        self.assertNotIn('private-token', str(error.exception))

    @patch('migrator.kubernetes.run')
    def test_snapshot_discovery_and_explicit_context(self, run):
        ns = {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': 'demo'}}
        cm = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'app', 'namespace': 'demo'}}
        run.side_effect = ['src\ndst\n', json.dumps({'items': [ns]}), 'configmaps cm v1 true ConfigMap create,get,list\n', json.dumps({'items': [cm]})]
        resources, missing = k.snapshot('src', ['demo'])
        self.assertEqual(resources, [ns, cm])
        self.assertIn('ActiveMQArtemis', missing)
        for call in run.call_args_list[1:]:
            self.assertEqual(call.args[0][:2], ['--context', 'src'])
        self.assertFalse(any('apply' in call.args[0] for call in run.call_args_list))

    @patch('migrator.kubernetes.run')
    def test_failed_list_is_not_empty_success(self, run):
        run.side_effect = ['src\n', json.dumps({'items': [{'metadata': {'name': 'demo'}}]}), 'secrets v1 true Secret get,list\n', k.ClusterError('Forbidden')]
        with self.assertRaises(k.ClusterError):
            k.snapshot('src', ['demo'])

    def test_invalid_selection(self):
        for source, target, namespaces in [('a','a',['demo']), ('a','b',['kube-system']), ('a','b',['--all-namespaces']), ('a','b',[])]:
            with self.assertRaises(ValueError):
                k.validate_selection(source, target, namespaces)

    @patch('migrator.kubernetes.snapshot')
    def test_live_report_masks_secrets(self, snapshot):
        obj={'apiVersion':'v1','kind':'Secret','metadata':{'name':'token','namespace':'demo'},'data':{'token':'PRIVATE_VALUE'}}
        snapshot.side_effect=[([obj], []), ([], [])]
        report=k.live_analyze({'source_context':'a','target_context':'b','namespaces':['demo']})
        self.assertEqual(report['summary']['SOURCE_ONLY'], 1)
        self.assertNotIn('PRIVATE_VALUE', json.dumps(report))

if __name__ == '__main__':
    unittest.main()
