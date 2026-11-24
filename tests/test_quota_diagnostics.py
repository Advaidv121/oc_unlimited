import importlib.util
import json
import sqlite3
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    'quota_diagnostics', Path(__file__).parents[1] / 'scripts/quota-diagnostics.py')
diagnostics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostics)


class QuotaDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.execute('CREATE TABLE session_message '
                                '(session_id TEXT,type TEXT,time_created INTEGER,data TEXT)')
        self.addCleanup(self.connection.close)

    def add(self, stamp, message, kind='assistant', sid='test'):
        self.connection.execute('INSERT INTO session_message VALUES (?,?,?,?)',
                                (sid, kind, stamp, json.dumps(message)))

    def failure(self, delay=60000):
        return dict(model=dict(id='test-model'), content='PRIVATE_CONTENT',
                    error=dict(type='provider.free-limit', status=429,
                               retryAfterMs=delay, response=dict(body='PRIVATE_BODY')))

    def test_counts_assistant_failures_without_idle_duplicates(self):
        self.add(1000, self.failure())
        self.add(1001, self.failure(), 'idle')
        self.add(1002, dict(error=dict(type='network')))
        report = diagnostics.summarize(self.connection, 0)
        self.assertEqual(report['quota_failure_count'], 1)
        self.assertEqual(report['failures_by_model'], {'test-model': 1})
        self.assertNotIn('PRIVATE', json.dumps(report))

    def test_previous_context_includes_cache_and_stays_with_session(self):
        self.add(500, dict(tokens=dict(input=10, cache=dict(read=100, write=5))))
        self.add(600, dict(tokens=dict(input=999)), sid='other')
        self.add(1000, self.failure())
        failure = diagnostics.summarize(self.connection, 900)['recent_failures'][0]
        self.assertEqual(failure['preceding_context_tokens'], 115)
        self.assertEqual(failure['retry_at_utc'], '1970-01-01T00:01:01+00:00')

    def test_missing_usage_is_unknown_not_zero(self):
        self.add(1000, self.failure(None))
        failure = diagnostics.summarize(self.connection, 0)['recent_failures'][0]
        self.assertIsNone(failure['preceding_context_tokens'])
        self.assertIsNone(failure['retry_at_utc'])

    def test_time_window(self):
        self.add(1000, self.failure())
        self.assertEqual(diagnostics.summarize(self.connection, 2000)['quota_failure_count'], 0)
