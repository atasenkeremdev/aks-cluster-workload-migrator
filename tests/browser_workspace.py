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
            self.assertIn('target / demo', page.locator('#sync-description').inner_text())
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
            page.click('#sync-confirm')
            page.wait_for_function("document.querySelector('#history-rows').textContent.includes('RollbackApplied')")
            self.assertEqual(state['target']['spec']['replicas'], 0)
            # Scope edits invalidate old actions, while the report keeps its original contexts.
            page.click('#step-scope')
            page.select_option('#dst', 'source')
            self.assertEqual(page.locator('[data-sync]').count(), 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
