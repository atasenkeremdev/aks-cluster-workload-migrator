import copy
import unittest
from migrator.core import analyze, normalize, difference_paths, reason


def deployment(image='old.example/app:v1'):
    return {'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':'app','namespace':'demo'},'spec':{'template':{'metadata':{'labels':{'openservicemesh.io/test':'yes'}},'spec':{'containers':[{'name':'app','image':image}]}}}}


class CorrectnessTests(unittest.TestCase):
    def test_target_old_registry_cannot_match(self):
        source=deployment();target=normalize(source)
        row=analyze([source],[target],['demo'],'old.example','new.example')['resources'][0]
        self.assertEqual(row['status'],'DIFF')
        self.assertIn('/spec/template/spec/containers/0/image',row['diff_paths'])

    def test_target_osm_not_hidden(self):
        source=deployment('new.example/app:v1')
        row=analyze([source],[source],['demo'])['resources'][0]
        self.assertEqual(row['status'],'DIFF')

    def test_only_exact_image_prefix_changes(self):
        source=deployment('old.example.evil/app:v1')
        self.assertEqual(normalize(source,'old.example','new.example')['spec']['template']['spec']['containers'][0]['image'],'old.example.evil/app:v1')
        source=deployment();source['spec']['template']['spec']['initContainers']=[{'image':'old.example/init:v1'}]
        source['metadata']['annotations']={'url':'old.example/keep'}
        result=normalize(source,'old.example','new.example')
        self.assertEqual(result['spec']['template']['spec']['initContainers'][0]['image'],'new.example/init:v1')
        self.assertEqual(result['metadata']['annotations']['url'],'old.example/keep')

    def test_operator_children_excluded(self):
        obj=deployment();obj['metadata']['ownerReferences']=[{'kind':'ActiveMQArtemis','controller':True}]
        self.assertIn('Controller-managed',reason(obj))
        result=analyze([obj],[obj],['demo'])
        self.assertEqual(result['resources'],[])
        self.assertEqual(len(result['skipped']),2)

    def test_configmap_secret_not_rewritten(self):
        for kind in ('ConfigMap','Secret'):
            obj={'apiVersion':'v1','kind':kind,'metadata':{'name':'app','namespace':'demo'},'data':{'value':'old.example'}}
            self.assertEqual(normalize(obj,'old.example','new.example'),obj)

    def test_absent_null_field_highlighted(self):
        self.assertEqual(difference_paths({'a':None},{}),['/a'])

    def test_absent_null_list_item_highlighted(self):
        self.assertEqual(difference_paths([None],[]),['/0'])
