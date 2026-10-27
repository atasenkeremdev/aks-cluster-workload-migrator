"""Azure role inventory and additive, confirmed target IAM sync safeguards."""
import copy
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch
from migrator import iam, azure_iam, iam_history, history, iam_jobs
from migrator.kubernetes import ClusterError

SOURCE_ID='/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/source/providers/Microsoft.ContainerService/managedClusters/source'
TARGET_ID='/subscriptions/22222222-2222-2222-2222-222222222222/resourceGroups/target/providers/Microsoft.ContainerService/managedClusters/target'
ROLE='acdd72a7-3385-48ef-bd42-f606fba81ae7'
PRINCIPAL='33333333-3333-3333-3333-333333333333'
RAW_RUN_JSON=azure_iam.run_json
TENANT='44444444-4444-4444-4444-444444444444'


def definition(scope):
    return {'id':scope.split('/resourceGroups/')[0]+'/providers/Microsoft.Authorization/roleDefinitions/'+ROLE,
            'roleName':'Reader','roleType':'BuiltInRole','assignableScopes':['/'],
            'permissions':[{'actions':['*/read'],'notActions':[],'dataActions':[],'notDataActions':[]}]}


def assignment(scope):
    return {'id':scope+'/providers/Microsoft.Authorization/roleAssignments/generated-assignment',
            'scope':scope,'principalId':PRINCIPAL,'principalType':'Group','roleDefinitionId':definition(scope)['id'],
            'condition':None,'conditionVersion':None}


class RoleIAMTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        env=patch.dict(os.environ,{'MIGRATOR_STATE_DIR':self.temp.name});env.start();self.addCleanup(env.stop)
        iam._PLANS.clear()
        self.body={'source_context':'source','target_context':'target','azure_source_id':SOURCE_ID,'azure_target_id':TARGET_ID}
        self.request={**self.body,'principal_id':PRINCIPAL,'role_id':ROLE}
        self.source=[assignment(SOURCE_ID)];self.target=[]
        self.source_defs=[definition(SOURCE_ID)];self.target_defs=[definition(TARGET_ID)]
        self.calls=[];self.writes=[];self.fail=None;self.cross_tenant=False;self.fail_reads=False
        runner=patch('migrator.azure_iam.run_json',side_effect=self.azure)
        runner.start();self.addCleanup(runner.stop)

    def azure(self,args):
        self.calls.append(args)
        if args[:2]==['cloud','show']:return 'https://graph.microsoft.com/'
        if args[0]=='rest':return {'value':[{'id':PRINCIPAL,'displayName':'Platform Readers','@odata.type':'#microsoft.graph.group'}]}
        source=args[args.index('--subscription')+1].startswith('1111')
        if args[:2]==['account','show']:
            return '55555555-5555-5555-5555-555555555555' if self.cross_tenant and not source else TENANT
        if self.fail_reads and not source and args[:3]==['role','assignment','list']:
            raise ClusterError('Azure denied access')
        if args[:3]==['role','assignment','list']:return copy.deepcopy(self.source if source else self.target)
        if args[:3]==['role','definition','list']:return copy.deepcopy(self.source_defs if source else self.target_defs)
        if args[:3]==['role','assignment','create']:
            self.assertEqual(args[args.index('--scope')+1],TARGET_ID)
            self.assertFalse(source)
            self.assertEqual(iam_history.listing()['operations'][0]['status'],'Applying')
            self.writes.append(args)
            if self.fail:raise self.fail
            self.target=[assignment(TARGET_ID)]
            return copy.deepcopy(self.target[0])
        raise AssertionError(args)

    def execute(self,token):
        return iam.sync_execute({'token':token,'confirm':True})

    def test_azure_roles_only_no_kubernetes_inventory(self):
        with patch('migrator.kubernetes.run') as kubectl:
            result=iam.analyze(self.body)
        kubectl.assert_not_called()
        self.assertTrue(result['complete']);row=result['resources'][0]
        self.assertEqual(row['name'],'Reader');self.assertEqual(row['status'],'SOURCE_ONLY');self.assertTrue(row['syncable'])
        self.assertTrue(all(args[:3] in (['role','assignment','list'],['role','definition','list']) or args[:2]==['cloud','show'] or (args[0]=='rest' and '/directoryObjects/getByIds' in args[args.index('--url')+1]) for args in self.calls))
        self.assertEqual(self.writes,[])

    def test_generated_assignment_ids_and_cluster_scope_ids_do_not_create_false_differences(self):
        self.target=[assignment(TARGET_ID)];self.target[0]['id']='different-generated-id'
        row=iam.analyze(self.body)['resources'][0]
        self.assertEqual(row['status'],'MATCH');self.assertFalse(row['syncable'])

    def test_inherited_and_direct_matching_grants_are_not_duplicated(self):
        self.target=[assignment(TARGET_ID),assignment(TARGET_ID)]
        self.target[1]['scope']=TARGET_ID.split('/resourceGroups/')[0]
        result=iam.analyze(self.body)
        self.assertEqual(result['summary']['MATCH'],1)
        self.assertTrue(result['resources'][0]['target']['assignments'][1]['inherited'])

    def test_inherited_source_is_visible_but_not_syncable(self):
        self.source[0]['scope']=SOURCE_ID.split('/resourceGroups/')[0]
        row=iam.analyze(self.body)['resources'][0]
        self.assertEqual(row['status'],'SOURCE_ONLY');self.assertFalse(row['syncable'])
        with self.assertRaises(ValueError):iam.sync_plan(self.request)
        self.assertEqual(self.writes,[])

    def test_read_failure_is_unknown_never_missing_target_permission(self):
        self.fail_reads=True;result=iam.analyze(self.body)
        self.assertFalse(result['complete']);self.assertEqual(result['summary']['UNKNOWN'],1)
        self.assertFalse(result['resources'][0]['syncable'])
        with self.assertRaises(ValueError):iam.sync_plan(self.request)

    def test_target_only_and_conditional_differences_are_not_overwritten(self):
        self.source=[];self.target=[assignment(TARGET_ID)]
        self.assertEqual(iam.analyze(self.body)['summary']['TARGET_ONLY'],1)
        self.source=[assignment(SOURCE_ID)];self.target[0]['condition']='condition'
        self.assertEqual(iam.analyze(self.body)['summary']['DIFF'],1)
        with self.assertRaises(ValueError):iam.sync_plan(self.request)
        self.assertEqual(self.writes,[])

    def test_custom_role_permissions_must_match_and_role_must_exist(self):
        self.target_defs[0]['permissions'][0]['actions']=['*']
        self.assertFalse(iam.analyze(self.body)['resources'][0]['syncable'])
        with self.assertRaises(ValueError):iam.sync_plan(self.request)
        self.target_defs=[]
        with self.assertRaises(ValueError):iam.sync_plan(self.request)

    def test_preview_is_read_only_and_execution_requires_confirmation(self):
        plan=iam.sync_plan(self.request)
        self.assertEqual(plan['grant']['roleName'],'Reader');self.assertIn('does not prove write authorization',plan['warning'])
        self.assertEqual(self.writes,[])
        with self.assertRaises(ValueError):iam.sync_execute({'token':plan['token']})
        result=self.execute(plan['token']);self.assertEqual(result['status'],'Applied')
        self.assertEqual(len(self.writes),1)
        self.assertIn('--assignee-object-id',self.writes[0]);self.assertIn('--assignee-principal-type',self.writes[0])
        self.assertEqual(iam_history.listing()['operations'][0]['status'],'Applied')
        self.assertEqual(history.listing()['operations'],[])
        with self.assertRaises(ValueError):self.execute(plan['token'])

    def test_stale_source_and_target_role_definitions_block_a_write(self):
        token=iam.sync_plan(self.request)['token']
        self.source=[]
        with self.assertRaises(ValueError):self.execute(token)
        self.source=[assignment(SOURCE_ID)];token=iam.sync_plan(self.request)['token']
        self.target_defs[0]['permissions'][0]['actions']=['*']
        with self.assertRaises(ValueError):self.execute(token)
        self.assertEqual(self.writes,[])

    def test_existing_target_grant_after_preview_is_idempotently_skipped(self):
        token=iam.sync_plan(self.request)['token'];self.target=[assignment(TARGET_ID)]
        self.assertEqual(self.execute(token)['status'],'AlreadyPresent');self.assertEqual(self.writes,[])

    def test_expired_cancelled_and_cross_tenant_previews_cannot_write(self):
        token=iam.sync_plan(self.request)['token'];iam._PLANS[token]['expires']=time.monotonic()-1
        with self.assertRaises(ValueError):self.execute(token)
        token=iam.sync_plan(self.request)['token'];iam.discard({'token':token})
        with self.assertRaises(ValueError):self.execute(token)
        self.cross_tenant=True
        with self.assertRaisesRegex(ValueError,'Cross-tenant'):iam.sync_plan(self.request)
        self.assertEqual(self.writes,[])

    def test_conditional_unknown_principal_and_unassignable_role_require_manual_review(self):
        for change in ('condition','principal','scope'):
            with self.subTest(change=change):
                self.source=[assignment(SOURCE_ID)];self.target_defs=[definition(TARGET_ID)]
                if change=='condition':self.source[0]['condition']='ABAC condition'
                if change=='principal':self.source[0]['principalType']=None
                if change=='scope':self.target_defs[0]['assignableScopes']=[SOURCE_ID]
                with self.assertRaises(ValueError):iam.sync_plan(self.request)
        self.assertEqual(self.writes,[])

    def test_uncertain_create_is_journaled_and_not_retried(self):
        token=iam.sync_plan(self.request)['token'];self.fail=ClusterError('Timed out')
        with self.assertRaisesRegex(ClusterError,'uncertain'):self.execute(token)
        self.assertEqual(iam_history.listing()['operations'][0]['status'],'Unknown')
        self.assertEqual(len(self.writes),1)

    def test_journal_failure_prevents_creating_a_grant(self):
        token=iam.sync_plan(self.request)['token']
        with patch('migrator.iam_history.save',side_effect=OSError('Disk full')):
            with self.assertRaises(OSError):self.execute(token)
        self.assertEqual(self.writes,[])

    def test_iam_jobs_remain_separate_and_sanitize_unexpected_errors(self):
        from migrator import jobs
        self.assertIsNot(iam_jobs._JOBS,jobs._JOBS)
        iam_jobs._JOBS['test']={'state':'running','started':0,'updated':0,'step_started':0,'events':[],'completed_commands':0}
        self.addCleanup(iam_jobs._JOBS.pop,'test',None)
        with patch('migrator.iam_jobs.analyze',side_effect=RuntimeError('PRIVATE')):iam_jobs._worker('test',self.body)
        self.assertNotIn('PRIVATE',json.dumps(iam_jobs.status('test')))

    def test_cli_failure_does_not_print_authentication_output(self):
        response=type('Response',(),{'returncode':1,'stdout':'','stderr':'PRIVATE az login token'})()
        with patch('migrator.azure_iam.shutil.which',return_value='/usr/bin/az'),patch('migrator.azure_iam.subprocess.run',return_value=response):
            with self.assertRaises(ClusterError) as caught:RAW_RUN_JSON(['role','assignment','list'])
        self.assertNotIn('PRIVATE',str(caught.exception))
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


class HumanReadableIAMTests(unittest.TestCase):
    USER='11111111-aaaa-aaaa-aaaa-111111111111'
    GROUP='22222222-bbbb-bbbb-bbbb-222222222222'
    MANAGED='33333333-cccc-cccc-cccc-333333333333'
    APP='44444444-dddd-dddd-dddd-444444444444'

    def row(self,principal,kind,role='Reader',status='SOURCE_ONLY'):
        return {'principal_id':principal,'name':role,'status':status,
                'source':{'assignments':[{'principalType':kind}]},
                'target':{'assignments':[{'principalType':kind}]} if status=='MATCH' else None}

    def test_names_user_group_and_managed_identity_types_are_resolved_without_secrets(self):
        from migrator import iam_principals
        rows=[self.row(self.USER,'User'),self.row(self.USER,'User','AKS RBAC Reader','MATCH'),self.row(self.GROUP,'Group'),self.row(self.MANAGED,'ServicePrincipal'),self.row(self.APP,'ServicePrincipal')]
        objects=[{'id':self.USER,'displayName':'Alex','userPrincipalName':'alex@example.com','@odata.type':'#microsoft.graph.user','password':'PRIVATE'},
                 {'id':self.GROUP,'displayName':'Platform Team','@odata.type':'#microsoft.graph.group'},
                 {'id':self.MANAGED,'displayName':'payments-identity','@odata.type':'#microsoft.graph.servicePrincipal','servicePrincipalType':'ManagedIdentity'},
                 {'id':self.APP,'displayName':'automation-app','@odata.type':'#microsoft.graph.servicePrincipal','servicePrincipalType':'Application'}]
        def query(args):
            if args[:2]==['cloud','show']:return 'https://graph.microsoft.com/'
            self.assertEqual(args[:3],['rest','--method','POST'])
            self.assertTrue(args[args.index('--url')+1].endswith('/directoryObjects/getByIds'))
            self.assertEqual(set(json.loads(args[args.index('--body')+1])['ids']),{self.USER,self.GROUP,self.MANAGED,self.APP})
            return {'value':objects}
        with patch('migrator.azure_iam.run_json',side_effect=query):identities=iam_principals.resolve(rows,SOURCE_ID,TARGET_ID)
        self.assertEqual(identities[self.MANAGED]['principal_type'],'ManagedIdentity')
        self.assertEqual(identities[self.APP]['principal_type'],'ServicePrincipal')
        self.assertEqual(identities[self.USER]['user_principal_name'],'alex@example.com')
        summaries=iam_principals.summarize(rows,identities,'aks-source','aks-target')
        user=next(item for item in summaries if item['principal_id']==self.USER)
        self.assertEqual(user['source_roles'],['AKS RBAC Reader','Reader'])
        self.assertEqual(user['target_roles'],['AKS RBAC Reader'])
        self.assertEqual(user['missing_roles'],['Reader'])
        self.assertIn('Missing role assignments on aks-target: Reader',user['summary'])
        self.assertTrue(any('Managed identity payments-identity' in item['summary'] for item in summaries))
        self.assertNotIn('PRIVATE',json.dumps(summaries))

    def test_directory_permission_failure_keeps_object_id_and_does_not_invent_names(self):
        from migrator import iam_principals
        rows=[self.row(self.GROUP,'Group')]
        with patch('migrator.azure_iam.run_json',side_effect=ClusterError('Directory access denied')):
            identities=iam_principals.resolve(rows,SOURCE_ID,TARGET_ID)
        summary=iam_principals.summarize(rows,identities,'source','target')[0]
        self.assertFalse(summary['name_resolved']);self.assertEqual(summary['display_name'],self.GROUP)
        self.assertIn('Display name could not be resolved',summary['summary'])
        self.assertEqual(rows[0]['status'],'SOURCE_ONLY')

    def test_unknown_coverage_does_not_claim_missing_roles(self):
        from migrator import iam_principals
        rows=[self.row(self.USER,'User',status='UNKNOWN')]
        with patch('migrator.azure_iam.run_json',side_effect=ClusterError('Unavailable')):
            identities=iam_principals.resolve(rows,SOURCE_ID,TARGET_ID)
        summary=iam_principals.summarize(rows,identities,'source','target')[0]
        self.assertEqual(summary['missing_roles'],[])
        self.assertIn('missing roles cannot be confirmed',summary['summary'])
        self.assertNotIn('No source role assignments are missing',summary['summary'])

    def test_identical_display_names_never_merge_distinct_principals(self):
        from migrator import iam_principals
        rows=[self.row(self.USER,'User'),self.row(self.GROUP,'User',status='MATCH')]
        identities={key:{'principal_id':key,'display_name':'Alex','principal_type':'User','name_resolved':True} for key in (self.USER,self.GROUP)}
        summaries=iam_principals.summarize(rows,identities,'source','target')
        self.assertEqual(len(summaries),2)
        self.assertEqual(next(item for item in summaries if item['principal_id']==self.USER)['missing_roles'],['Reader'])
        self.assertEqual(next(item for item in summaries if item['principal_id']==self.GROUP)['missing_roles'],[])

    def test_directory_objects_not_in_the_requested_batch_are_not_exported(self):
        from migrator import iam_principals
        rows=[self.row(self.USER,'User')]
        def query(args):
            return 'https://graph.microsoft.com/' if args[:2]==['cloud','show'] else {'value':[{'id':self.APP,'displayName':'UNREQUESTED'}]}
        with patch('migrator.azure_iam.run_json',side_effect=query):identities=iam_principals.resolve(rows,SOURCE_ID,TARGET_ID)
        self.assertNotIn('UNREQUESTED',json.dumps(identities));self.assertFalse(identities[self.USER]['name_resolved'])
