import copy
import json
import unittest
from migrator.core import analyze, normalize


def resource(kind='Deployment', name='app', **extra):
    return {'apiVersion': 'apps/v1', 'kind': kind, 'metadata': {'name': name, 'namespace': 'demo'}, **extra}


class MigrationTests(unittest.TestCase):
    def test_statefulset_retains_parent_and_all_claims(self):
        obj = resource('StatefulSet', spec={'volumeClaimTemplates': [
            {'metadata': {'name': name, 'uid': 'runtime'}, 'spec': {'storageClassName': 'fast'}, 'status': {}}
            for name in ['data', 'logs']]})
        result = normalize(obj)
        self.assertEqual(result['kind'], 'StatefulSet')
        self.assertEqual(len(result['spec']['volumeClaimTemplates']), 2)
        self.assertNotIn('uid', result['spec']['volumeClaimTemplates'][0]['metadata'])
        self.assertIn('uid', obj['spec']['volumeClaimTemplates'][0]['metadata'])
        self.assertEqual(normalize(resource('StatefulSet'))['kind'], 'StatefulSet')

    def test_expected_transformations_match(self):
        src = resource(spec={'template': {'metadata': {'labels': {'openservicemesh.io/x': 'y'}}, 'spec': {'containers': [{'image': 'old.example/a'}]}}})
        dst = normalize(src, 'old.example', 'new.example')
        self.assertEqual(analyze([src], [dst], ['demo'], 'old.example', 'new.example')['summary']['MATCH'], 1)

    def test_registry_is_literal(self):
        obj = resource(spec={'image': 'oldXexample/a'})
        self.assertEqual(normalize(obj, 'old.example', 'new.example'), obj)

    def test_policy_shared(self):
        resources = [resource('NetworkPolicy'), resource('ActiveMQArtemis'), resource('Secret', type='kubernetes.io/service-account-token'), resource('Ingress')]
        report = analyze(resources, resources, ['demo'])
        self.assertEqual(report['summary']['MATCH'], 2)
        self.assertEqual(len(report['skipped']), 4)

    def test_secret_comparison_redacts_values(self):
        src = resource('Secret', data={'password': 'SOURCE_SECRET'})
        dst = resource('Secret', data={'password': 'TARGET_SECRET'})
        result = analyze([src], [dst], ['demo'])
        self.assertEqual(result['summary']['DIFF'], 1)
        self.assertNotIn('SOURCE_SECRET', json.dumps(result))
        self.assertNotIn('TARGET_SECRET', json.dumps(result))

    def test_statuses(self):
        a, b, c = [resource(name=n) for n in ['a', 'b', 'c']]
        result = analyze([a, b], [a, c], ['demo'])
        self.assertEqual(result['summary'], {'MATCH': 1, 'DIFF': 0, 'SOURCE_ONLY': 1, 'TARGET_ONLY': 1})

    def test_headless_service_preserved(self):
        obj = resource('Service', spec={'clusterIP': 'None', 'clusterIPs': ['None']})
        self.assertEqual(normalize(obj)['spec']['clusterIP'], 'None')
        obj['spec']['clusterIP'] = '10.0.0.1'
        self.assertNotIn('clusterIP', normalize(obj)['spec'])

    def test_protected_and_duplicate_rejected(self):
        with self.assertRaises(ValueError):
            analyze([], [], ['kube-system'])
        a = resource()
        with self.assertRaises(ValueError):
            analyze([a, a], [], ['demo'])

    def test_invalid_resource_rejected(self):
        with self.assertRaises(ValueError):
            analyze([{}], [], ['demo'])

if __name__ == '__main__':
    unittest.main()
