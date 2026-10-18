import threading
import unittest
from unittest.mock import patch
from migrator import jobs
from migrator.kubernetes import ClusterError, progress

BODY={'source_context':'source','target_context':'target','namespaces':['demo']}


class JobTests(unittest.TestCase):
    def setUp(self):
        jobs._JOBS.clear()

    def test_running_step_visible_before_completion(self):
        entered, release = threading.Event(), threading.Event()
        def analyze(_):
            progress('Waiting: source / demo · get Deployment', 'waiting')
            entered.set()
            release.wait(3)
            progress('Completed: source / demo · get Deployment', 'completed')
            return {'summary': {'MATCH': 1}}
        with patch('migrator.jobs.live_analyze', side_effect=analyze):
            job_id=jobs.start(BODY)['job_id']
            self.assertTrue(entered.wait(2))
            running=jobs.status(job_id)
            self.assertEqual(running['state'],'running')
            self.assertEqual(running['phase'],'waiting')
            self.assertIn('Deployment',running['current'])
            release.set()
            self.wait_terminal(job_id)
            final=jobs.status(job_id)
            self.assertEqual(final['state'],'completed')
            self.assertEqual(final['completed_commands'],1)
            self.assertEqual(final['result']['summary']['MATCH'],1)

    def wait_terminal(self, job_id):
        # Event.wait releases the interpreter while the worker finishes.
        for _ in range(100):
            if jobs.status(job_id)['state']!='running':
                return
            threading.Event().wait(.01)
        self.fail('Worker did not finish')

    def test_timeout_preserves_failure_and_step_history(self):
        def analyze(_):
            progress('Waiting: target / demo · get Secret','waiting')
            raise ClusterError('Target cluster: request timed out')
        with patch('migrator.jobs.live_analyze',side_effect=analyze):
            job_id=jobs.start(BODY)['job_id']
            self.wait_terminal(job_id)
        result=jobs.status(job_id)
        self.assertEqual(result['state'],'failed')
        self.assertIn('timed out',result['error'])
        self.assertIn('get Secret',result['events'][0]['message'])
        self.assertNotIn('result',result)

    def test_unknown_job(self):
        with self.assertRaises(ClusterError) as error:
            jobs.status('missing')
        self.assertEqual(error.exception.status,404)

    def test_unexpected_exception_does_not_leak(self):
        with patch('migrator.jobs.live_analyze',side_effect=RuntimeError('PRIVATE_SECRET')):
            job_id=jobs.start(BODY)['job_id']
            self.wait_terminal(job_id)
        self.assertNotIn('PRIVATE_SECRET',str(jobs.status(job_id)))

    def test_invalid_selection_starts_no_job(self):
        with self.assertRaises(ValueError):
            jobs.start({**BODY,'namespaces':['kube-system']})
        self.assertFalse(jobs._JOBS)
