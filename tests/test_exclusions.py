"""Startup namespace policy and backend bypass prevention."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from migrator import core
from migrator.kubernetes import validate_selection, sync_plan, namespaces


class ExclusionTests(unittest.TestCase):
    def setUp(self):
        self.old = set(core.STATIC_EXCLUSIONS)
        self.addCleanup(self.restore)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'exclusions.txt'

    def restore(self):
        core.STATIC_EXCLUSIONS.clear()
        core.STATIC_EXCLUSIONS.update(self.old)
        core.PROTECTED.clear()
        core.PROTECTED.update(core.BUILTIN_PROTECTED | self.old)

    def test_file_parsing_and_all_migration_entry_points(self):
        self.path.write_text('# comment\n\nlegacy\nlegacy # duplicate\n', encoding='utf-8')
        core.load_exclusions(self.path)
        self.assertEqual(core.STATIC_EXCLUSIONS, {'legacy'})
        obj = {'apiVersion': 'v1', 'kind': 'ConfigMap', 'metadata': {'name': 'settings', 'namespace': 'legacy'}}
        self.assertEqual(core.reason(obj), 'Protected namespace')
        with self.assertRaises(ValueError):
            core.analyze([obj], [], ['legacy'])
        with self.assertRaises(ValueError):
            validate_selection('source', 'target', ['legacy'])
        with patch('migrator.kubernetes.run') as run:
            with self.assertRaises(ValueError):
                sync_plan({'source_context':'source', 'target_context':'target', 'namespace':'legacy', 'name':'settings', 'kind':'ConfigMap'})
            run.assert_not_called()
        with patch('migrator.kubernetes.context_args', return_value=['--context', 'source']), patch('migrator.kubernetes.read_json', return_value={'items':[{'metadata':{'name':'legacy'}}, {'metadata':{'name':'app'}}]}):
            result = namespaces('source')
        self.assertEqual(result['namespaces'], ['app'])
        self.assertEqual(result['protected_namespaces'], ['legacy'])

    def test_invalid_and_missing_files_preserve_policy(self):
        for value in ['UPPERCASE', '*', 'a' * 64, 'two names']:
            self.path.write_text(value, encoding='utf-8')
            with self.assertRaises(ValueError):
                core.load_exclusions(self.path)
        with self.assertRaises(OSError):
            core.load_exclusions(self.path.with_name('missing'))
        self.assertEqual(core.STATIC_EXCLUSIONS, self.old)

    def test_empty_file_cannot_remove_builtin_protection(self):
        self.path.write_text('', encoding='utf-8')
        core.load_exclusions(self.path)
        self.assertEqual(core.PROTECTED, core.BUILTIN_PROTECTED)
