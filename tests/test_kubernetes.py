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

class DiagnosticTests(unittest.TestCase):
    @patch('migrator.kubernetes.run', return_value='source-cluster\n')
    def test_unknown_context_has_actionable_message(self, _):
        with self.assertRaises(k.ClusterError) as error:
            k.context_args('target-cluster')
        self.assertEqual(error.exception.status, 400)
        self.assertIn('Load cluster contexts', str(error.exception))

    @patch('migrator.kubernetes.snapshot')
    def test_target_error_identifies_side(self, snapshot):
        snapshot.side_effect = [([], []), k.ClusterError('Forbidden')]
        with self.assertRaises(k.ClusterError) as error:
            k.live_analyze({'source_context':'src', 'target_context':'dst', 'namespaces':['demo']})
        self.assertEqual(str(error.exception), 'Target cluster: Forbidden')

class NamespaceDiagnosticsTests(unittest.TestCase):
    @patch('migrator.kubernetes.shutil.which', return_value='/bin/kubectl')
    @patch('migrator.kubernetes.subprocess.run')
    def test_connectivity_errors_are_actionable_and_redacted(self, run, _):
        cases = [('dial tcp: lookup API: no such host', 'DNS'),
                 ('Unable to connect to the server: connection refused', 'unreachable'),
                 ('x509: certificate signed by unknown authority', 'TLS')]
        for stderr, expected in cases:
            with self.subTest(stderr=stderr):
                run.return_value = subprocess.CompletedProcess([], 1, '', stderr + ' private-token-value')
                with self.assertRaises(k.ClusterError) as caught:
                    k.run(['--context', 'source-cluster', 'get', 'namespaces', '-o', 'json'])
                self.assertIn(expected, str(caught.exception))
                self.assertNotIn('private-token-value', str(caught.exception))

    @patch('migrator.kubernetes.context_args', return_value=['--context', 'source-cluster'])
    @patch('migrator.kubernetes.read_json', side_effect=k.ClusterError('Kubernetes denied access (RBAC Forbidden).', 502, 'permission'))
    def test_namespace_error_preserves_cause_and_adds_local_diagnostic(self, _, __):
        with self.assertRaises(k.ClusterError) as caught:
            k.namespaces('source-cluster')
        self.assertEqual(caught.exception.status, 502)
        self.assertEqual(caught.exception.category, 'permission')
        self.assertIn('RBAC Forbidden', str(caught.exception))
        self.assertIn('same terminal', str(caught.exception))
