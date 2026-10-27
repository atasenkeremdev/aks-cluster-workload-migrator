"""Portable connection export and private container initialization."""
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
from container import entrypoint,export_connections

TENANT='11111111-1111-1111-1111-111111111111'


class ContainerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.source=self.root/'mounted';self.source.write_text('ORIGINAL-CONFIG')
        env=patch.dict(os.environ,{'MIGRATOR_KUBECONFIG_SECRET':str(self.source),'KUBECONFIG':str(self.root/'runtime'/'config'),
            'MIGRATOR_STATE_DIR':str(self.root/'state'),'MIGRATOR_AZURE_AUTH':'none','MIGRATOR_AUTH_MODE':'local'})
        env.start();self.addCleanup(env.stop)

    def test_startup_copies_read_only_secret_privately_without_modifying_it(self):
        entrypoint.prepare();target=Path(os.environ['KUBECONFIG'])
        self.assertEqual(self.source.read_text(),'ORIGINAL-CONFIG');self.assertEqual(target.read_text(),'ORIGINAL-CONFIG')
        self.assertEqual(target.stat().st_mode&0o777,0o600)

    def test_signed_in_mode_cannot_be_combined_with_shared_credentials(self):
        with patch.dict(os.environ,{'MIGRATOR_AUTH_MODE':'device-code','MIGRATOR_AZURE_AUTH':'service-principal'}):
            with self.assertRaises(ValueError):entrypoint.prepare()

    def test_export_filters_contexts_and_removes_local_credentials(self):
        config={'clusters':[{'name':'a','cluster':{'server':'https://a.example'}},{'name':'b','cluster':{'server':'https://b.example'}}],
                'contexts':[{'name':'source','context':{'cluster':'a','user':'admin'}},{'name':'other','context':{'cluster':'b','user':'admin'}}],
                'users':[{'name':'admin','user':{'client-key-data':'PRIVATE','token':'PRIVATE'}}]}
        output=self.root/'connections.json'
        with patch('container.export_connections.subprocess.run',return_value=types.SimpleNamespace(stdout=json.dumps(config))):
            export_connections.export(TENANT,['source'],output)
        result=json.loads(output.read_text());self.assertNotIn('PRIVATE',output.read_text())
        self.assertEqual([item['name'] for item in result['contexts']],['source']);self.assertEqual([item['name'] for item in result['clusters']],['a'])
        self.assertEqual(output.stat().st_mode&0o777,0o600)
        with patch('container.export_connections.subprocess.run',return_value=types.SimpleNamespace(stdout=json.dumps(config))):
            with self.assertRaises(FileExistsError):export_connections.export(TENANT,['source'],output)

    def test_export_rejects_unknown_context_before_creating_a_file(self):
        output=self.root/'missing'
        with patch('container.export_connections.subprocess.run',return_value=types.SimpleNamespace(stdout='{}')):
            with self.assertRaises(ValueError):export_connections.export(TENANT,['missing'],output)
        self.assertFalse(output.exists())
