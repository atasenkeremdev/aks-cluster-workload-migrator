import json
import threading
import unittest
from http.client import HTTPConnection
from migrator import kubernetes as k
from migrator.server import Handler
from http.server import ThreadingHTTPServer


class PreviewDiscardTests(unittest.TestCase):
    def setUp(self):
        k._PLANS.clear()

    def test_discard_only_selected_sync_tokens(self):
        k._PLANS.update(a={'expires':1},b={'expires':1},rollback={'rollback':{'id':'x'}})
        self.assertEqual(k.discard_plans({'tokens':['a','missing','rollback']}),{'discarded':1})
        self.assertIn('b',k._PLANS)
        self.assertIn('rollback',k._PLANS)

    def test_invalid_discard_payload_returns_http_400(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            client=HTTPConnection('127.0.0.1',server.server_port)
            client.request('POST','/api/sync/discard',json.dumps({'tokens':'invalid'}),{'Content-Type':'application/json'})
            response=client.getresponse()
            self.assertEqual(response.status,400)
            self.assertIn('100',json.loads(response.read())['error'])
            client.close()
        finally:
            server.shutdown();server.server_close()
