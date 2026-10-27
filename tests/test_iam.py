"""Isolated, read-only IAM comparisons with honest partial coverage."""
import copy
import json
import unittest
from unittest.mock import patch
from migrator import iam, azure_iam, iam_jobs
from migrator.kubernetes import ClusterError

SOURCE_ID = '/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/source/providers/Microsoft.ContainerService/managedClusters/source'
TARGET_ID = '/subscriptions/22222222-2222-2222-2222-222222222222/resourceGroups/target/providers/Microsoft.ContainerService/managedClusters/target'


def role(name='read', namespace='demo'):
    return {'metadata':{'name':name,'namespace':namespace}, 'rules':[{'verbs':['get','list'],'apiGroups':[''],'resources':['pods']}]}


class IAMTests(unittest.TestCase):
    def setUp(self):
        self.body = {'source_context':'source','target_context':'target','namespaces':[]}
        self.context_patch = patch('migrator.iam.context_args', side_effect=lambda context:['--context',context])
        self.context_patch.start();self.addCleanup(self.context_patch.stop)

    def test_projection_does_not_return_tokens_or_arbitrary_annotations(self):
        value = {'metadata':{'name':'app','namespace':'demo','annotations':{
            'azure.workload.identity/client-id':'public-client','unsafe':'DO-NOT-EXPOSE'}},
            'secrets':[{'name':'token-secret'}], 'token':'DO-NOT-EXPOSE','imagePullSecrets':[{'name':'registry'}]}
        entry = iam.inventory_entry(value,'ServiceAccount')
        self.assertEqual(entry['configuration']['workload_identity']['azure.workload.identity/client-id'],'public-client')
        self.assertNotIn('DO-NOT-EXPOSE',json.dumps(entry))
        self.assertNotIn('token-secret',json.dumps(entry))

    def test_compare_ignores_rule_order_and_runtime_metadata(self):
        def read(args):
            source = args[1]=='source'
            if 'roles.rbac.authorization.k8s.io' in args:
                value=role();value['metadata']['uid']='different-per-cluster'
                if not source:value['rules'][0]['verbs'].reverse()
                return {'items':[value]}
            return {'items':[]}
        with patch('migrator.iam.read_json',side_effect=read):
            report=iam.analyze(self.body)
        self.assertTrue(report['complete']);self.assertEqual(report['summary']['MATCH'],1)
        self.assertEqual(report['resources'][0]['diff_paths'],[])

    def test_serviceaccount_identity_difference_and_binding_subjects(self):
        def read(args):
            if 'serviceaccounts' in args:
                return {'items':[{'metadata':{'name':'app','namespace':'demo','annotations':{
                    'azure.workload.identity/client-id':args[1]+'-client'}}}]}
            if 'rolebindings.rbac.authorization.k8s.io' in args:
                return {'items':[{'metadata':{'name':'read','namespace':'demo'},'roleRef':{'kind':'Role','name':'read','apiGroup':'rbac.authorization.k8s.io'},
                                  'subjects':[{'kind':'ServiceAccount','name':'app','namespace':'demo'}]}]}
            return {'items':[]}
        with patch('migrator.iam.read_json',side_effect=read):report=iam.analyze(self.body)
        self.assertEqual(report['summary']['DIFF'],1);self.assertEqual(report['summary']['MATCH'],1)
        self.assertIn('/workload_identity/azure.workload.identity~1client-id', next(row for row in report['resources'] if row['status']=='DIFF')['diff_paths'])

    def test_permission_failure_is_unknown_not_source_only(self):
        def read(args):
            if 'roles.rbac.authorization.k8s.io' in args:
                if args[1]=='target':raise ClusterError('Kubernetes denied access (RBAC Forbidden).')
                return {'items':[role()]}
            return {'items':[]}
        with patch('migrator.iam.read_json',side_effect=read):report=iam.analyze(self.body)
        self.assertFalse(report['complete']);self.assertEqual(report['summary']['UNKNOWN'],1)
        self.assertEqual(report['summary']['SOURCE_ONLY'],0)

    def test_missing_and_extra_resources_are_compared_only_after_successful_reads(self):
        def read(args):
            return {'items':[role(args[1])]} if 'roles.rbac.authorization.k8s.io' in args else {'items':[]}
        with patch('migrator.iam.read_json',side_effect=read):report=iam.analyze(self.body)
        self.assertEqual(report['summary']['SOURCE_ONLY'],1);self.assertEqual(report['summary']['TARGET_ONLY'],1)

    def test_scope_excludes_protected_namespaces_and_reads_cluster_rbac(self):
        calls=[]
        def read(args):
            calls.append(args)
            if 'serviceaccounts' in args:
                return {'items':[{'metadata':{'name':'system','namespace':'kube-system'}}, {'metadata':{'name':'app','namespace':'demo'}}]}
            return {'items':[]}
        with patch('migrator.iam.read_json',side_effect=read):report=iam.analyze({**self.body,'namespaces':['demo']})
        self.assertEqual(len(report['resources']),1)
        self.assertTrue(all('get' in args for args in calls))
        self.assertTrue(any('clusterroles.rbac.authorization.k8s.io' in args for args in calls))
        self.assertTrue(all('--all-namespaces' not in args for args in calls))

    def test_cluster_context_failure_does_not_hide_other_side(self):
        with patch('migrator.iam.context_args',side_effect=lambda context: (_ for _ in ()).throw(ClusterError('Unavailable')) if context=='source' else ['--context',context]), patch('migrator.iam.read_json',return_value={'items':[role()]}):
            # Return valid resources only for Roles; other resource shapes are not accepted here.
            with patch('migrator.iam.read_json',side_effect=lambda args:{'items':[role()]} if 'roles.rbac.authorization.k8s.io' in args else {'items':[]}):
                report=iam.analyze(self.body)
        self.assertEqual(report['summary']['UNKNOWN'],1);self.assertFalse(report['complete'])

    def test_invalid_inputs_never_read_a_cluster(self):
        for values in [{'source_context':'target'}, {'namespaces':['kube-system']}, {'namespaces':'demo'},
                       {'namespaces':['demo','demo']}, {'include_azure':True}, {'include_azure':'yes'}]:
            with self.subTest(values=values), patch('migrator.iam.read_json') as read:
                with self.assertRaises(ValueError):iam.analyze({**self.body,**values})
                read.assert_not_called()

    def test_azure_results_and_coverage_are_kept_in_iam_report(self):
        entry={'kind':'AKSIdentity','name':'control-plane','namespace':'','configuration':{'principalId':'public-id'},'system_managed':False}
        with patch('migrator.iam.read_json',return_value={'items':[]}), patch('migrator.azure_iam.collect',return_value=({('AKSIdentity','','control-plane'):entry},[{'kind':'AKSIdentity','scope':'','state':'complete','count':1}])) as azure:
            report=iam.analyze({**self.body,'include_azure':True,'azure_source_id':SOURCE_ID,'azure_target_id':TARGET_ID})
        self.assertEqual(azure.call_count,2);self.assertEqual(report['resources'][0]['provider'],'Azure')
        self.assertEqual(report['summary']['MATCH'],1)

    def test_iam_worker_has_separate_state_and_sanitizes_unexpected_errors(self):
        from migrator import jobs
        self.assertIsNot(iam_jobs._JOBS,jobs._JOBS)
        iam_jobs._JOBS['test']={'state':'running','started':0,'updated':0,'step_started':0,'events':[],'completed_commands':0}
        self.addCleanup(iam_jobs._JOBS.pop,'test',None)
        with patch('migrator.iam_jobs.analyze',side_effect=RuntimeError('PRIVATE-ERROR')):
            iam_jobs._worker('test',self.body)
        result=iam_jobs.status('test')
        self.assertEqual(result['state'],'failed');self.assertNotIn('PRIVATE-ERROR',json.dumps(result))


class AzureIAMTests(unittest.TestCase):
    def setUp(self):
        self.identity_id='/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/identities/providers/Microsoft.ManagedIdentity/userAssignedIdentities/app'
        self.calls=[]
        self.identity={'id':self.identity_id,'name':'app','resourceGroup':'identities','clientId':'client','principalId':'principal','tenantId':'tenant','unsafe':'PRIVATE'}

    def cli_run(self,args):
        self.calls.append(args)
        if args[:2]==['aks','show']:
            return {'enableRbac':True,'aadProfile':{'enableAzureRbac':True,'tenantId':'tenant','unsafe':'PRIVATE'},
                    'identity':{'type':'SystemAssigned','principalId':'control','tenantId':'tenant'},
                    'oidcIssuerProfile':{'issuerURL':'https://issuer.example'},'unsafe':'PRIVATE'}
        if args[:2]==['identity','list']:return [copy.deepcopy(self.identity)]
        if args[:3]==['identity','federated-credential','list']:
            return [{'name':'app-federation','issuer':'https://issuer.example','subject':'system:serviceaccount:demo:app','audiences':['api://AzureADTokenExchange'],'unsafe':'PRIVATE'}]
        if args[:3]==['role','assignment','list']:
            return [{'principalId':'principal','principalType':'ServicePrincipal','roleDefinitionId':'/roles/reader','scope':SOURCE_ID,'unsafe':'PRIVATE'}]
        raise AssertionError(args)

    def test_azure_inventory_projects_metadata_and_uses_only_read_commands(self):
        with patch('migrator.azure_iam.run_json',side_effect=self.cli_run):entries,coverage=azure_iam.collect(SOURCE_ID,{'client'})
        self.assertTrue(all(check['state']=='complete' for check in coverage))
        self.assertNotIn('PRIVATE',json.dumps(list(entries.values())))
        self.assertIn('FederatedCredential',{key[0] for key in entries})
        self.assertTrue(all('list' in args or 'show' in args for args in self.calls))
        self.assertTrue(all('--subscription' in args for args in self.calls))
        self.assertTrue(any('--assignee-object-id' in args for args in self.calls))
        self.assertTrue(any('--include-inherited' in args for args in self.calls))

    def test_azure_denied_access_is_partial_not_empty_success(self):
        with patch('migrator.azure_iam.run_json',side_effect=ClusterError('Azure denied access')):
            entries,coverage=azure_iam.collect(SOURCE_ID,set())
        self.assertEqual(entries,{})
        self.assertTrue(all(check['state']=='unavailable' for check in coverage))

    def test_unresolved_workload_identity_cannot_be_called_complete(self):
        with patch('migrator.azure_iam.run_json',side_effect=self.cli_run):entries,coverage=azure_iam.collect(SOURCE_ID,{'not-in-subscription'})
        self.assertEqual(next(check for check in coverage if check['kind']=='ManagedIdentity')['state'],'unavailable')
        self.assertEqual(next(check for check in coverage if check['kind']=='FederatedCredential')['state'],'unavailable')

    def test_cli_error_does_not_expose_stderr_and_never_logs_in(self):
        response=type('Response',(),{'returncode':1,'stdout':'','stderr':'PRIVATE az login token'})()
        with patch('migrator.azure_iam.shutil.which',return_value='/usr/bin/az'), patch('migrator.azure_iam.subprocess.run',return_value=response) as run:
            with self.assertRaises(ClusterError) as caught:azure_iam.run_json(['aks','show','--ids',SOURCE_ID])
        self.assertNotIn('PRIVATE',str(caught.exception));self.assertIn('authentication',str(caught.exception))
        self.assertEqual(run.call_args.args[0][1:3],['aks','show'])
        self.assertNotIn('shell',run.call_args.kwargs)


class ContextResolutionTests(unittest.TestCase):
    def resolve(self, endpoint, clusters, failure=False):
        def azure(args):
            if args[:2]==['account','list']:return ['11111111-1111-1111-1111-111111111111']
            if failure:raise ClusterError('Azure denied access')
            return clusters
        with patch('migrator.kubernetes.context_args',return_value=['--context','alias']), patch('migrator.kubernetes.run',return_value=endpoint) as kubectl, patch('migrator.azure_iam.run_json',side_effect=azure):
            result=azure_iam.resolve_context({'context':'alias'})
        self.assertIn('jsonpath={.clusters[0].cluster.server}',kubectl.call_args.args[0])
        self.assertNotIn('--raw',kubectl.call_args.args[0])
        return result

    def test_alias_context_matches_public_endpoint_without_name_guessing(self):
        result=self.resolve('https://API.EXAMPLE.:443',[{'id':SOURCE_ID,'fqdn':'api.example'}])
        self.assertEqual(result['resource_id'],SOURCE_ID)
        self.assertEqual(result['context'],'alias')

    def test_private_endpoint_is_supported(self):
        result=self.resolve('https://private.example',[{'id':SOURCE_ID,'fqdn':'public.example','privateFqdn':'private.example'}])
        self.assertEqual(result['resource_id'],SOURCE_ID)

    def test_no_match_and_ambiguous_matches_require_manual_entry(self):
        self.assertIsNone(self.resolve('https://elsewhere.example',[{'id':SOURCE_ID,'fqdn':'api.example'}])['resource_id'])
        self.assertIsNone(self.resolve('https://api.example',[{'id':SOURCE_ID,'fqdn':'api.example'},{'id':TARGET_ID,'fqdn':'api.example'}])['resource_id'])

    def test_permission_failure_does_not_guess_an_id(self):
        self.assertIsNone(self.resolve('https://api.example',[],failure=True)['resource_id'])

    def test_unsafe_endpoint_never_queries_azure(self):
        for endpoint in ('http://api.example','https://user:password@api.example','not-a-url'):
            with self.subTest(endpoint=endpoint),patch('migrator.kubernetes.context_args',return_value=['--context','alias']),patch('migrator.kubernetes.run',return_value=endpoint),patch('migrator.azure_iam.run_json') as azure:
                result=azure_iam.resolve_context({'context':'alias'})
                self.assertIsNone(result['resource_id']);azure.assert_not_called()

    def test_incomplete_subscription_discovery_does_not_claim_unique_match(self):
        def azure(args):
            if args[:2]==['account','list']:return ['one','two']
            if args[args.index('--subscription')+1]=='two':raise ClusterError('Forbidden')
            return [{'id':SOURCE_ID,'fqdn':'api.example'}]
        with patch('migrator.kubernetes.context_args',return_value=['--context','alias']),patch('migrator.kubernetes.run',return_value='https://api.example'),patch('migrator.azure_iam.run_json',side_effect=azure):
            result=azure_iam.resolve_context({'context':'alias'})
        self.assertIsNone(result['resource_id']);self.assertIn('could not be read',result['message'])
