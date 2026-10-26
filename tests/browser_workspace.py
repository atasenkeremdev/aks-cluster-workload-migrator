"""Optional browser regression suite: python3 tests/browser_workspace.py.
Requires Playwright and Chromium; runtime application has no such dependency.
"""
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from playwright.sync_api import sync_playwright
from migrator.server import Handler
from migrator.core import analyze
from migrator.kubernetes import ClusterError


class WorkspaceBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'MIGRATOR_STATE_DIR': self.temp.name})
        self.env.start()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.playwright = sync_playwright().start()
        executable = os.environ.get('BROWSER_EXECUTABLE') or shutil.which('chromium')
        options = {'headless': True}
        if executable:
            options['executable_path'] = executable
        if sys.platform.startswith('linux'):
            options['args'] = ['--no-sandbox']
        self.browser = self.playwright.chromium.launch(**options)
        self.page = self.browser.new_page(viewport={'width': 1500, 'height': 1100})
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.page.goto(f'http://127.0.0.1:{self.server.server_port}')

    def tearDown(self):
        self.browser.close()
        self.playwright.stop()
        self.server.shutdown()
        self.server.server_close()
        self.env.stop()
        self.temp.cleanup()
        self.assertFalse(self.errors)

    def test_comparison_filters_navigation_and_responsive_layout(self):
        page = self.page
        def obj(name, value='1'):
            return {'apiVersion':'v1', 'kind':'ConfigMap', 'metadata':{'name':name, 'namespace':'demo'}, 'data':{'value':value}}
        report = analyze([obj('matching'), obj('different'), obj('source-only')],
                         [obj('matching'), obj('different', '2'), obj('target-only')], ['demo'])
        self.assertEqual(page.locator('html').get_attribute('lang'),'en')
        self.assertNotRegex(page.locator('body').inner_text(),r'[çğıöşüÇĞİÖŞÜ]')
        self.assertEqual(page.locator('#mode').input_value(), 'live')
        page.locator('#advanced>summary').click()
        page.select_option('#mode', 'offline')
        page.click('#demo')
        page.route('**/api/analyze', lambda route: route.fulfill(json=report))
        page.click('#submit')
        page.wait_for_selector('#results', state='visible')
        self.assertFalse(page.locator('#scope').evaluate('(element)=>element.open'))
        self.assertEqual(page.locator('#rows .resource-row').count(), 3)
        row = page.locator('#rows tr').filter(has_text='different')
        self.assertEqual(row.locator('td').count(), 3)
        row.locator('td').nth(1).get_by_role('button', name='different', exact=True).click()
        self.assertGreater(page.locator('#source-preview .diff-line').count(), 0)
        self.assertGreater(page.locator('#target-preview .diff-line').count(), 0)
        page.fill('#resource-search', 'source-only')
        self.assertEqual(page.locator('#rows .resource-row').count(), 1)
        page.click('#clear-filters')
        page.select_option('#filter-status', 'MATCH')
        self.assertFalse(page.locator('#differences-only').is_checked())
        self.assertEqual(page.locator('#rows .resource-row').count(), 1)
        page.click('#clear-filters')
        page.uncheck('#differences-only')
        self.assertEqual(page.locator('#rows .resource-row').count(), 4)
        self.assertEqual(page.locator('#sync-mode').count(),0)
        self.assertEqual(page.locator('#update-strategy').count(),1)
        page.click('#step-scope')
        page.fill('#new', 'another.example')
        page.locator('#old').click()
        self.assertTrue(page.locator('#scope-stale').is_visible())
        page.set_viewport_size({'width':390, 'height':844})
        self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=window.innerWidth'))

    def test_namespace_exclusions_are_visible_and_limit_analysis(self):
        page = self.page
        page.locator('#advanced>summary').click()
        page.select_option('#mode', 'offline')
        page.click('#demo')
        objects = [{'apiVersion': 'v1', 'kind': 'ConfigMap',
                    'metadata': {'name': 'settings', 'namespace': name},
                    'data': {'key': 'value'}}
                   for name in ['alpha', 'beta', 'kube-system']]
        page.fill('#source', json.dumps(objects))
        page.fill('#target', '[]')
        page.click('#namespace-from-json')
        page.click('#namespace-summary')
        page.check('#namespace-all')
        page.uncheck('#namespace-list input[value="beta"]')
        page.click('#namespace-summary')
        self.assertIn('1 namespaces included · 2 excluded', page.locator('#overview-namespaces').inner_text())
        page.locator('#namespace-exclusions-summary').click()
        text = page.locator('#namespace-exclusions-list').inner_text()
        self.assertIn('beta — User selection', text)
        self.assertIn('kube-system — System policy', text)
        self.assertTrue(page.locator('#namespace-list input[value="kube-system"]').is_disabled())
        page.click('#submit')
        page.wait_for_selector('#results', state='visible')
        self.assertEqual(page.locator('#rows tr').count(), 1)
        self.assertIn('alpha', page.locator('#rows').inner_text())
        self.assertTrue(page.locator('#namespace-exclusions').is_visible())
        with page.expect_download() as download_info:
            page.click('#download')
        downloaded = json.loads(Path(download_info.value.path()).read_text())
        self.assertEqual(downloaded['namespace_scope']['included'], ['alpha'])
        self.assertEqual(downloaded['namespace_scope']['excluded'], [
            {'name': 'beta', 'reason': 'User selection'},
            {'name': 'kube-system', 'reason': 'System policy'}])
        page.click('#step-scope')
        page.click('#namespace-summary')
        page.check('#namespace-list input[value="beta"]')
        self.assertTrue(page.locator('#scope-stale').is_visible())
        self.assertNotIn('beta', page.locator('#namespace-exclusions-list').inner_text())
        page.select_option('#mode', 'live')
        self.assertTrue(page.locator('#namespace-exclusions').is_hidden())

    def test_file_and_additional_exclusions_disable_picker_and_limit_scope(self):
        page = self.page
        page.route('**/api/namespace-policy', lambda route: route.fulfill(json={
            'protected_namespaces':['kube-system','legacy'], 'static_exclusions':['legacy']}))
        page.reload()
        page.locator('#advanced>summary').click()
        page.select_option('#mode', 'offline')
        page.click('#demo')
        objects = [{'apiVersion':'v1', 'kind':'ConfigMap',
                    'metadata':{'name':'settings','namespace':name}, 'data':{'key':'value'}}
                   for name in ['alpha','beta','legacy','kube-system']]
        page.fill('#source', json.dumps(objects))
        page.fill('#target', '[]')
        page.click('#namespace-from-json')
        page.click('#namespace-summary')
        page.check('#namespace-all')
        self.assertTrue(page.locator('#namespace-list input[value="legacy"]').is_disabled())
        self.assertTrue(page.locator('#namespace-list input[value="kube-system"]').is_disabled())
        page.click('#namespace-summary')
        page.fill('#additional-exclusions', 'beta')
        self.assertTrue(page.locator('#namespace-list input[value="beta"]').is_disabled())
        page.click('#submit')
        page.wait_for_selector('#results', state='visible')
        self.assertEqual(page.locator('#rows tr').count(), 1)
        self.assertIn('alpha', page.locator('#rows').inner_text())
        page.click('#step-scope')
        page.fill('#additional-exclusions', '')
        self.assertFalse(page.locator('#namespace-list input[value="beta"]').is_disabled())
        self.assertFalse(page.locator('#namespace-list input[value="beta"]').is_checked())
        self.assertEqual(self.errors, [])

    def test_namespace_select_all_and_live_waiting_progress(self):
        page = self.page
        complete = threading.Event()
        submitted = []
        page.route('**/api/contexts', lambda route: route.fulfill(json={'contexts':['source','target']}))
        page.route('**/api/namespaces', lambda route: route.fulfill(json={'namespaces':['alpha','beta','gamma']}))
        def start(route):
            submitted.append(route.request.post_data_json)
            route.fulfill(json={'job_id':'progress-test'})
        def status(route):
            done = complete.is_set()
            result = {'state':'completed' if done else 'running', 'current':'Completed' if done else 'Waiting: source / alpha · get Deployment',
                      'phase':'completed_analysis' if done else 'waiting', 'elapsed_seconds':2, 'step_seconds':1, 'completed_commands':1,
                      'events':[{'elapsed':1, 'message':'Waiting: source / alpha · get Deployment'}]}
            if done:
                result['result'] = {'resources':[], 'summary':{}, 'notice':'test', 'skipped':[]}
            route.fulfill(json=result)
        page.route('**/api/live/jobs', start)
        page.route('**/api/jobs/progress-test', status)
        page.click('#connect')
        page.wait_for_function("document.querySelector('#dst').options.length===3")
        page.select_option('#src', 'source')
        page.select_option('#dst', 'target')
        page.click('#load-namespaces')
        page.wait_for_function("document.querySelectorAll('#namespace-list input').length===3")
        page.click('#namespace-summary')
        page.fill('#namespace-search', 'alpha')
        page.check('#namespace-all')
        self.assertIn('3 namespace', page.locator('#namespace-summary-text').inner_text())
        page.click('#namespace-summary')
        page.click('#submit')
        page.wait_for_function("document.querySelector('#progress-current').textContent.includes('Waiting: source')")
        self.assertTrue(page.locator('#analysis-progress').is_visible())
        self.assertIn('Waiting', page.locator('#progress-wait').inner_text())
        self.assertIn('Analyzing', page.locator('#overview-activity').inner_text())
        self.assertFalse(page.locator('#analysis-progress details').evaluate('(element)=>element.open'))
        self.assertEqual(submitted[0]['namespaces'], ['alpha','beta','gamma'])
        complete.set()
        page.wait_for_selector('#results', state='visible')
        self.assertTrue(page.locator('#analysis-progress').evaluate('(element)=>element.classList.contains("finished")'))

    def test_configuration_uses_sync_and_cancelled_preview_cannot_write(self):
        page = self.page
        secret={'apiVersion':'v1','kind':'Secret','metadata':{'name':'credentials','namespace':'demo'},'data':{'password':'ONE'}}
        target=copy.deepcopy(secret);target['data']['password']='TWO'
        report=analyze([secret],[target],['demo'])
        requests=[]
        writes=[]
        page.route('**/api/contexts',lambda route:route.fulfill(json={'contexts':['source','target']}))
        page.route('**/api/namespaces',lambda route:route.fulfill(json={'namespaces':['demo']}))
        page.route('**/api/live/jobs',lambda route:route.fulfill(json={'job_id':'config-sync'}))
        page.route('**/api/jobs/config-sync',lambda route:route.fulfill(json={'state':'completed','current':'Complete','phase':'completed_analysis','elapsed_seconds':1,'step_seconds':0,'completed_commands':1,'events':[],'result':report}))
        page.route('**/api/sync/plan',lambda route:requests.append(route))
        page.route('**/api/sync/execute',lambda route:(writes.append(route.request.post_data_json),route.fulfill(json={'status':'Applied','operation_id':'config-test'})))
        page.route('**/api/history',lambda route:route.fulfill(json={'operations':[]}))
        page.route('**/api/operations/health',lambda route:route.fulfill(json={'state':'Applied','detail':'Configuration accepted'}))
        page.click('#connect');page.wait_for_function("document.querySelector('#dst').options.length===3")
        page.select_option('#src','source');page.select_option('#dst','target');page.click('#load-namespaces')
        page.wait_for_function("document.querySelector('#namespace-list input')!==null")
        page.click('#namespace-summary');page.check('#namespace-all');page.click('#namespace-summary');page.click('#submit')
        page.wait_for_selector('#results',state='visible')
        self.assertEqual(page.locator('#rows [data-sync]').all_text_contents(),['Sync to target'])
        page.locator('#rows [data-action=sync]').click();page.wait_for_selector('#sync-dialog',state='visible')
        self.assertEqual(page.locator('#confirm-title').inner_text(),'Review configuration sync')
        self.assertTrue(page.locator('#sync-confirm').is_disabled())
        page.click('#sync-cancel')
        page.wait_for_timeout(50)
        self.assertEqual(requests[0].request.post_data_json['mode'],'sync')
        preview={'token':'first','mode':'sync','manifest':report['resources'][0]['manifest'],'operation':'update','target_context':'target','warnings':[]}
        requests[0].fulfill(json=preview)
        page.wait_for_timeout(50)
        self.assertFalse(page.locator('#sync-dialog').evaluate('(element)=>element.open'))
        self.assertEqual(writes,[])
        page.locator('#rows [data-action=sync]').click()
        page.wait_for_timeout(50)
        requests[1].fulfill(json={**preview,'token':'second'})
        page.wait_for_function("!document.querySelector('#sync-confirm').disabled")
        self.assertFalse(page.locator('#review-advanced').evaluate('(element)=>element.open'))
        self.assertNotIn('activate',page.locator('#confirm-impact').inner_text().lower())
        page.click('#sync-confirm')
        page.wait_for_function("document.querySelector('#overview-activity').textContent==='Applied'")
        self.assertEqual(writes,[{'token':'second','confirm':True}])

    def test_bulk_actions_filter_validation_and_partial_failure(self):
        page=self.page
        def obj(kind,name):
            return {'apiVersion':'apps/v1' if kind=='Deployment' else 'batch/v1' if kind=='CronJob' else 'v1','kind':kind,'metadata':{'name':name,'namespace':'demo'}}
        configs=[obj('ConfigMap','config-a'),obj('ConfigMap','config-c'),obj('Secret','config-b')]
        workloads=[obj('Deployment','worker'),obj('CronJob','schedule')]
        matching=obj('ConfigMap','matching')
        report=analyze([*configs,*workloads,matching],[matching,obj('ConfigMap','target-only')],['demo'])
        plans=[];writes=[];tokens={}
        page.route('**/api/contexts',lambda route:route.fulfill(json={'contexts':['source','target']}))
        page.route('**/api/namespaces',lambda route:route.fulfill(json={'namespaces':['demo']}))
        page.route('**/api/live/jobs',lambda route:route.fulfill(json={'job_id':'bulk'}))
        page.route('**/api/jobs/bulk',lambda route:route.fulfill(json={'state':'completed','current':'Complete','phase':'completed_analysis','elapsed_seconds':1,'step_seconds':0,'completed_commands':1,'events':[],'result':report}))
        def plan(route):
            body=route.request.post_data_json;plans.append(body)
            if body['name']=='config-b' and body['strategy']=='apply':
                route.fulfill(status=409,json={'error':'Server-side apply field ownership conflict. Field manager(s): Mozilla. Nothing was applied.','category':'field_ownership'});return
            token='plan-'+str(len(plans));tokens[token]=body
            route.fulfill(json={'token':token,'mode':body['mode'],'strategy':body['strategy'],'manifest':{'kind':body['kind']},'warnings':[],'expires_in':300})
        def execute(route):
            body=tokens[route.request.post_data_json['token']];writes.append(body)
            if len(writes)==2:
                route.fulfill(status=409,json={'error':'Target configuration changed','category':'resource_version'});return
            route.fulfill(json={'status':'Applied','operation_id':'bulk-one'})
        page.route('**/api/sync/plan',plan);page.route('**/api/sync/execute',execute)
        page.route('**/api/history',lambda route:route.fulfill(json={'operations':[]}))
        page.click('#connect');page.wait_for_function("document.querySelector('#dst').options.length===3")
        page.select_option('#src','source');page.select_option('#dst','target');page.click('#load-namespaces')
        page.wait_for_function("document.querySelector('#namespace-list input')!==null")
        page.click('#namespace-summary');page.check('#namespace-all');page.click('#namespace-summary');page.click('#submit')
        page.wait_for_selector('#results',state='visible')
        self.assertEqual(page.locator('#bulk-sync').inner_text(),'Sync all (3)')
        self.assertEqual(page.locator('#bulk-activate').inner_text(),'Activate all (2)')
        page.select_option('#filter-kind','Secret');self.assertFalse(page.locator('#bulk-actions').is_visible())
        page.click('#clear-filters');page.click('#bulk-activate')
        page.wait_for_function("!document.querySelector('#sync-confirm').disabled")
        self.assertEqual({body['name'] for body in plans},{'worker','schedule'})
        self.assertTrue(all(body['mode']=='activate' for body in plans))
        page.click('#sync-cancel');self.assertEqual(writes,[])
        plans.clear();page.click('#bulk-sync');page.wait_for_selector('#preview-guarded',state='visible')
        self.assertTrue(page.locator('#sync-confirm').is_disabled());self.assertEqual(writes,[])
        self.assertIn('Mozilla',page.locator('#conflict-summary').inner_text())
        page.click('#preview-guarded');page.wait_for_function("!document.querySelector('#sync-confirm').disabled")
        self.assertEqual(page.locator('#bulk-review-rows tr').count(),3)
        self.assertTrue(all(body['mode']=='sync' for body in plans))
        self.assertTrue(all(body['strategy']=='merge' for body in plans[-3:]))
        page.click('#sync-confirm');page.wait_for_function("document.querySelector('#review-status').textContent.includes('Stopped after 1 of 3')")
        self.assertEqual(len(writes),2)
        self.assertIn('Not processed',page.locator('#bulk-review-rows').inner_text())
        self.assertTrue(page.locator('#sync-confirm').is_disabled())
        page.click('#sync-cancel');self.assertFalse(page.locator('#bulk-actions').is_visible())
        # A fresh comparison re-enables eligible actions; successful activation
        # applies every reviewed workload and keeps the per-resource results.
        page.click('#step-scope');page.click('#submit');page.wait_for_selector('#results',state='visible')
        page.click('#bulk-activate');page.wait_for_function("!document.querySelector('#sync-confirm').disabled")
        page.click('#sync-confirm');page.wait_for_function("document.querySelector('#review-status').textContent.includes('Applied 2 resources')")
        self.assertEqual([body['mode'] for body in writes[-2:]],['activate','activate'])
        self.assertEqual(page.locator('#bulk-review-rows').inner_text().count('Applied'),2)
        self.assertTrue(page.locator('#sync-confirm').is_disabled())

    def test_bulk_source_scale_filters_validation_cancel_partial_failure_and_success(self):
        page = self.page
        def workload(name, kind='Deployment', replicas=3):
            return {'apiVersion':'apps/v1','kind':kind,'metadata':{'name':name,'namespace':'demo'},
                    'spec':{'replicas':replicas,'template':{'spec':{'containers':[{'name':'app','image':'example/app:1'}]}}}}
        source = [workload('a'),workload('b'),workload('c','StatefulSet'),workload('matching'),workload('zero',replicas=0)]
        target = copy.deepcopy(source)
        for obj in target[:3]:
            obj['spec']['replicas'] = 1
        target.append(workload('target-only'))
        source.append({'apiVersion':'batch/v1','kind':'CronJob','metadata':{'name':'schedule','namespace':'demo'},'spec':{'suspend':False}})
        report = analyze(source,target,['demo'])
        state = {'fail_preview':False,'fail_write':False,'writes':[], 'plans':[], 'discarded':[], 'target_writes':[]}
        tokens = {}
        page.route('**/api/contexts',lambda route:route.fulfill(json={'contexts':['source','target']}))
        page.route('**/api/namespaces',lambda route:route.fulfill(json={'namespaces':['demo']}))
        page.route('**/api/live/jobs',lambda route:route.fulfill(status=202,json={'job_id':'source-bulk'}))
        page.route('**/api/jobs/source-bulk',lambda route:route.fulfill(json={'state':'completed','current':'Complete','phase':'completed_analysis','elapsed_seconds':1,'step_seconds':0,'completed_commands':1,'events':[],'result':report}))
        page.route('**/api/history',lambda route:route.fulfill(json={'operations':[]}))
        def plan(route):
            body = route.request.post_data_json
            state['plans'].append(body)
            if state['fail_preview'] and body['name']=='b':
                route.fulfill(status=400,json={'error':'A source HPA controls this workload.'});return
            token = 'source-plan-'+str(len(state['plans']))
            tokens[token] = body
            route.fulfill(json={'token':token,'mode':'source-scale-zero','previous_replicas':3,
                                'manifest':{'spec':{'replicas':0}},'expires_in':300,'warnings':[]})
        def execute(route):
            body = tokens[route.request.post_data_json['token']]
            self.assertTrue(route.request.post_data_json['confirm'])
            state['writes'].append(body)
            if state['fail_write'] and body['name']=='b':
                route.fulfill(status=409,json={'error':'Source changed after preview.','category':'resource_version'});return
            route.fulfill(json={'status':'Applied','operation_id':body['name']})
        def discard(route):
            state['discarded'].extend(route.request.post_data_json['tokens'])
            route.fulfill(json={'discarded':len(route.request.post_data_json['tokens'])})
        def target_write(route):
            state['target_writes'].append(route.request.post_data_json)
            route.fulfill(status=400,json={'error':'Unexpected target write'})
        page.route('**/api/source/scale/plan',plan);page.route('**/api/source/scale/execute',execute)
        page.route('**/api/sync/discard',discard);page.route('**/api/sync/execute',target_write)
        page.click('#connect');page.wait_for_function("document.querySelector('#dst').options.length===3")
        page.select_option('#src','source');page.select_option('#dst','target');page.click('#load-namespaces')
        page.wait_for_function("document.querySelector('#namespace-list input')!==null")
        page.click('#namespace-summary');page.check('#namespace-all');page.click('#namespace-summary')
        page.click('#submit');page.wait_for_selector('#results',state='visible')
        button = page.locator('#bulk-source-scale-zero')
        self.assertEqual(button.inner_text(),'Scale all source to zero (3)')
        self.assertEqual(page.locator('#rows [data-action="source-scale-zero"]').count(),3)
        page.uncheck('#differences-only')
        self.assertEqual(button.inner_text(),'Scale all source to zero (4)')
        page.fill('#resource-search','matching')
        self.assertTrue(button.is_hidden())
        self.assertEqual(page.locator('#rows [data-action="source-scale-zero"]').count(),1)
        page.click('#clear-filters')
        page.select_option('#filter-kind','StatefulSet');self.assertTrue(button.is_hidden())
        page.select_option('#filter-kind','Deployment');self.assertEqual(button.inner_text(),'Scale all source to zero (2)')
        page.click('#clear-filters')
        # One blocked workload prevents every write, including previously validated items.
        state['fail_preview'] = True
        button.click();page.wait_for_selector('#review-conflict',state='visible')
        self.assertIn('HPA',page.locator('#conflict-summary').inner_text())
        self.assertTrue(page.locator('#sync-confirm').is_disabled())
        self.assertEqual(state['writes'],[])
        self.assertGreaterEqual(len(state['discarded']),1)
        page.click('#sync-cancel');state['fail_preview'] = False
        # Cancellation after successful previews still makes no writes.
        button.click();page.wait_for_function("!document.querySelector('#sync-confirm').disabled")
        self.assertEqual(page.locator('#review-role').inner_text(),'SOURCE CLUSTER CHANGE')
        self.assertEqual(page.locator('#review-cluster').inner_text(),'source')
        self.assertEqual(page.locator('#review-change').inner_text(),'3 source workloads → 0 replicas')
        self.assertEqual(page.locator('#bulk-review-rows tr').count(),3)
        self.assertEqual(page.locator('#bulk-review-rows').inner_text().count('3 → 0 replicas'),3)
        self.assertTrue(page.locator('#strategy-control').is_hidden())
        page.click('#sync-cancel');self.assertEqual(state['writes'],[])
        # The second write fails: retain the first, do not process the third.
        state['fail_write'] = True
        button.click();page.wait_for_function("!document.querySelector('#sync-confirm').disabled")
        page.click('#sync-confirm')
        page.wait_for_function("document.querySelector('#review-status').textContent.includes('Stopped after 1 of 3')")
        self.assertEqual([body['name'] for body in state['writes']],['a','b'])
        self.assertIn('Not processed',page.locator('#bulk-review-rows').inner_text())
        page.click('#sync-cancel');self.assertTrue(button.is_hidden())
        # Re-analysis is required; the successful batch keeps the source endpoint.
        state['fail_write'] = False;state['writes'].clear()
        page.click('#step-scope');page.click('#submit');page.wait_for_selector('#results',state='visible')
        button.click();page.wait_for_function("!document.querySelector('#sync-confirm').disabled")
        page.click('#sync-confirm')
        page.wait_for_function("document.querySelector('#review-status').textContent.includes('Applied 3 resources')")
        self.assertEqual([body['name'] for body in state['writes']],['a','b','c'])
        self.assertTrue(all(body['source_context']=='source' for body in state['writes']))
        self.assertEqual(state['target_writes'],[])
        self.assertTrue(page.locator('#sync-confirm').is_disabled())
        page.click('#sync-cancel')
        # The existing batch limit is retained for source shutdown.
        report.clear();report.update(analyze([workload('worker-'+str(i)) for i in range(101)],[],['demo']))
        page.click('#step-scope');page.click('#submit');page.wait_for_selector('#results',state='visible')
        self.assertEqual(button.inner_text(),'Scale all source to zero (101)')
        self.assertTrue(button.is_disabled())

    def test_source_scale_preview_cancel_confirm_and_stale_scope(self):
        page = self.page
        source = {'apiVersion':'apps/v1','kind':'Deployment',
                  'metadata':{'name':'worker','namespace':'demo','uid':'source-uid','resourceVersion':'1','generation':1},
                  'spec':{'replicas':2,'template':{'spec':{'containers':[{'name':'worker','image':'example/worker:1'}]}}}}
        state = {'source':copy.deepcopy(source), 'writes':[]}
        target = copy.deepcopy(source)
        target['metadata']['uid'] = 'target-uid'
        def run(args, payload=None):
            if 'get-contexts' in args:
                return 'source\ntarget\n'
            context = args[args.index('--context')+1]
            if 'api-resources' in args:
                return 'deployments deploy apps/v1 true Deployment get,list\n'
            if 'patch' in args:
                self.assertEqual(context, 'source')
                self.assertIn('--subresource=scale', args)
                if '--dry-run=server' not in args:
                    state['writes'].append(copy.deepcopy(payload))
                    state['source']['spec']['replicas'] = 0
                    state['source']['metadata'].update(resourceVersion='2', generation=2)
                    state['source']['status'] = {'observedGeneration':2,'replicas':0}
                return json.dumps({'apiVersion':'autoscaling/v1','kind':'Scale',
                                   'metadata':state['source']['metadata'], 'spec':{'replicas':0}})
            kind = args[args.index('get')+1]
            if kind == 'namespaces':
                return json.dumps({'items':[{'apiVersion':'v1','kind':'Namespace','metadata':{'name':'demo'}}]})
            if kind == 'horizontalpodautoscalers':
                self.assertEqual(context, 'source')
                return '{"items":[]}'
            current = state['source'] if context == 'source' else target
            return json.dumps(current if 'worker' in args else {'items':[current]})
        with patch('migrator.kubernetes.run', side_effect=run):
            page.click('#connect')
            page.wait_for_function("document.querySelector('#src').options.length===3")
            page.select_option('#src','source');page.select_option('#dst','target')
            page.click('#load-namespaces')
            page.wait_for_function("document.querySelector('#namespace-list input')!==null")
            page.click('#namespace-summary');page.check('#namespace-all');page.click('#namespace-summary')
            page.click('#submit');page.wait_for_selector('#results',state='visible')
            # Identical source and target workloads may still need source shutdown.
            page.uncheck('#differences-only')
            scale = page.locator('#rows [data-action="source-scale-zero"]')
            self.assertEqual(scale.count(), 1)
            self.assertEqual(page.locator('#rows [data-action="activate"]').count(), 0)
            scale.click()
            page.wait_for_function("!document.querySelector('#sync-confirm').disabled")
            self.assertEqual(page.locator('#review-role').inner_text(),'SOURCE CLUSTER CHANGE')
            self.assertEqual(page.locator('#sync-description').inner_text(),'Deployment/worker')
            self.assertEqual(page.locator('#review-cluster').inner_text(),'source')
            self.assertEqual(page.locator('#review-namespace').inner_text(),'demo')
            self.assertEqual(page.locator('#review-change').inner_text(),'2 → 0 replicas')
            self.assertEqual(page.locator('#rows td:nth-child(2) .source-actions').count(),1)
            self.assertEqual(page.locator('#rows td:nth-child(3) .source-actions').count(),0)
            self.assertTrue(page.locator('#review-advanced').is_visible())
            self.assertFalse(page.locator('#review-advanced').evaluate('(element)=>element.open'))
            page.set_viewport_size({'width':390,'height':844})
            self.assertTrue(page.locator('#review-change').is_visible())
            self.assertTrue(page.locator('#sync-dialog').evaluate('(element)=>element.scrollWidth<=element.clientWidth'))
            page.screenshot(path='/tmp/source-scale-review-mobile.png')
            page.set_viewport_size({'width':1500,'height':1100})
            page.screenshot(path='/tmp/source-scale-review-desktop.png')
            self.assertTrue(page.locator('#strategy-control').is_hidden())
            self.assertEqual(state['writes'], [])
            page.click('#sync-cancel')
            self.assertEqual(state['writes'], [])
            scale.click();page.wait_for_function("!document.querySelector('#sync-confirm').disabled")
            # A programmatic scope change models configuration changing while a review is open.
            page.evaluate("document.querySelector('#old').value='old.registry';document.querySelector('#new').value='new.registry'")
            page.click('#sync-confirm')
            page.wait_for_function("document.querySelector('#review-error').textContent.includes('Scope changed')")
            self.assertIn('Scope changed', page.locator('#conflict-summary').inner_text())
            self.assertEqual(state['writes'], [])
            page.click('#sync-cancel')
            page.click('#step-scope');page.click('#submit')
            page.wait_for_selector('#results',state='visible');page.uncheck('#differences-only')
            page.locator('#rows [data-action="source-scale-zero"]').click()
            page.wait_for_function("!document.querySelector('#sync-confirm').disabled")
            page.click('#sync-confirm');page.wait_for_selector('#sync-dialog',state='hidden')
            page.wait_for_function("document.querySelector('#history-rows').textContent.includes('ScaledDown')")
            self.assertEqual(len(state['writes']), 1)
            self.assertEqual(state['source']['spec']['replicas'], 0)
            self.assertEqual(target['spec']['replicas'], 2)
            page.locator('#history-details > summary').click()
            self.assertIn('SOURCE: source',page.locator('#history-rows').inner_text())
            self.assertIn('replicas 2 → 0',page.locator('#history-rows').inner_text())
            self.assertEqual(page.locator('#rows [data-action="source-scale-zero"]').count(), 0)

    def test_live_prepare_activate_and_rollback(self):
        page = self.page
        namespace = {'apiVersion':'v1','kind':'Namespace','metadata':{'name':'demo'}}
        source = {'apiVersion':'apps/v1','kind':'Deployment','metadata':{'name':'app','namespace':'demo','uid':'source','resourceVersion':'1','generation':1},
                  'spec':{'replicas':2,'template':{'spec':{'containers':[{'name':'app','image':'registry/app:v1'}]}}}}
        state = {'target':None, 'version':1}
        def run(args, payload=None):
            if 'get-contexts' in args:
                return 'source\ntarget\n'
            if 'api-resources' in args:
                return 'deployments deploy apps/v1 true Deployment get,list\n'
            if any(action in args for action in ('create', 'apply', 'replace', 'patch')):
                if 'apply' in args and '--dry-run=server' in args and payload['spec']['replicas']==2:
                    raise ClusterError('Server-side apply field ownership conflict. Field manager: terraform.',409,'field_ownership')
                result = copy.deepcopy(payload)
                result['metadata'].update(uid='target', resourceVersion=str(state['version']+1), generation=state['version']+1)
                replicas = result['spec']['replicas']
                result['status'] = {'observedGeneration':result['metadata']['generation'], 'replicas':replicas, 'readyReplicas':replicas, 'updatedReplicas':replicas, 'availableReplicas':replicas}
                if '--dry-run=server' not in args:
                    state['version'] += 1
                    state['target'] = result
                return json.dumps(result)
            kind = args[args.index('get')+1]
            if kind == 'namespaces':
                return json.dumps({'items':[namespace]})
            if kind == 'horizontalpodautoscalers':
                return '{"items":[]}'
            current = source if args[args.index('--context')+1] == 'source' else state['target']
            if '--ignore-not-found' in args:
                return json.dumps(current) if current else ''
            if 'app' in args:
                return json.dumps(current)
            return json.dumps({'items':[current] if current else []})
        with patch('migrator.kubernetes.run', side_effect=run):
            page.click('#connect')
            page.wait_for_function("document.querySelector('#dst').options.length===3")
            page.select_option('#src', 'source')
            page.select_option('#dst', 'target')
            page.click('#load-namespaces')
            page.wait_for_function("document.querySelector('#namespace-list input')!==null")
            page.click('#namespace-summary')
            page.check('#namespace-all')
            page.click('#namespace-summary')
            page.click('#submit')
            page.wait_for_selector('#results', state='visible')
            page.locator('#rows [data-action=prepare]').click()
            page.wait_for_selector('#sync-dialog', state='visible')
            self.assertIn('passive', page.locator('#confirm-title').inner_text())
            self.assertEqual(page.locator('#review-cluster').inner_text(),'target')
            self.assertEqual(page.locator('#review-namespace').inner_text(),'demo')
            self.assertEqual(page.locator('#review-role').inner_text(),'TARGET CLUSTER CHANGE')
            self.assertEqual(page.locator('#review-change').inner_text(),'Set target replicas to 0')
            self.assertEqual(page.locator('#rows td:nth-child(2) .target-actions').count(),0)
            self.assertEqual(page.locator('#rows td:nth-child(3) [data-action=prepare]').count(),1)
            page.click('#sync-confirm')
            page.wait_for_function("document.querySelector('#history-rows').textContent.includes('Prepared')")
            self.assertEqual(state['target']['spec']['replicas'], 0)
            page.click('#step-scope');page.click('#submit');page.wait_for_selector('#results',state='visible')
            self.assertEqual(page.locator('#rows [data-action=activate]').inner_text(),'Activate target')
            page.locator('#rows [data-action=activate]').click()
            page.wait_for_selector('#preview-guarded', state='visible')
            self.assertEqual(state['target']['spec']['replicas'],0)
            page.click('#preview-guarded')
            page.wait_for_selector('#sync-dialog', state='visible')
            page.wait_for_function("document.querySelector('#sync-warnings').textContent.includes('overwrites submitted fields')")
            self.assertIn('duplicate processing', page.locator('#confirm-impact').inner_text())
            page.click('#sync-confirm')
            page.wait_for_function("document.querySelector('#overview-activity').textContent==='Ready'")
            self.assertEqual(state['target']['spec']['replicas'], 2)
            page.click('#step-execute')
            page.locator('#history-details>summary').click()
            page.locator('#history-rows tr').first.get_by_text('Preview rollback').click()
            page.wait_for_selector('#sync-dialog', state='visible')
            self.assertIn('pre-sync', page.locator('#confirm-impact').inner_text())
            self.assertEqual(page.locator('#review-change').inner_text(),'Restore previous configuration')
            self.assertEqual(page.locator('#review-cluster').inner_text(),'target')
            page.click('#sync-confirm')
            page.wait_for_function("document.querySelector('#history-rows').textContent.includes('RollbackApplied')")
            self.assertEqual(state['target']['spec']['replicas'], 0)
            # Scope edits invalidate old actions, while the report keeps its original contexts.
            page.click('#step-scope')
            page.select_option('#dst', 'source')
            self.assertEqual(page.locator('[data-sync]').count(), 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
