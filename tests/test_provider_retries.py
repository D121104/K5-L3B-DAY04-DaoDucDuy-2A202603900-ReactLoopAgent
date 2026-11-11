"""Offline transport retries and safe failure propagation."""
import io
import json
from pathlib import Path
import sys
import traceback
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import unittest
import providers
from unittest.mock import Mock, call, patch
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'evals'))
from providers import ProviderError, post_json, safe_error_details
from agent import run_agent
from grading import grade_case
import run_evals


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        patch.dict(providers._ENDPOINTS, {}, clear=True).start()
        patch.dict('os.environ', {'PROVIDER_MIN_INTERVAL': '1',
                                 'PROVIDER_RATE_LIMIT_COOLDOWN': '15',
                                 'PROVIDER_MAX_WAIT': '180'}).start()
        patch('providers.time.monotonic', side_effect=lambda: self.now).start()
        self.sleep = patch('providers.time.sleep', side_effect=self.advance).start()
        self.network = patch('providers.urllib.request.urlopen').start()
        self.addCleanup(patch.stopall)
        self.response = Mock()
        self.response.__enter__ = Mock(return_value=io.BytesIO(b'{"ok":true}'))
        self.response.__exit__ = Mock(return_value=False)

    def advance(self, seconds):
        self.now += seconds

    def reset_pacing(self):
        providers._ENDPOINTS.clear()
        self.sleep.reset_mock()

    def http(self, status, retry_after=None):
        return HTTPError('https://secret.invalid/key', status, 'SECRET reason',
                         {'Retry-After': retry_after} if retry_after else {},
                         io.BytesIO(b'SECRET body'))

    def request(self):
        headers = {'Authorization': 'SECRET'}
        payload = {'input': 'SECRET'}
        return post_json('https://example.invalid', headers, payload)

    def test_transient_http_success(self):
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.reset_pacing()
                self.network.side_effect = [self.http(status), self.response]
                self.response.__enter__.return_value = io.BytesIO(b'{"ok":true}')
                self.assertEqual(self.request(), {'ok': True})
                self.sleep.assert_called_once_with(15 if status == 429 else 1)

    def test_transient_connections_success(self):
        for error in (URLError(ConnectionResetError('SECRET')),
                      URLError(TimeoutError('SECRET')), TimeoutError('SECRET'),
                      ConnectionRefusedError('SECRET')):
            with self.subTest(error=type(error).__name__):
                self.network.side_effect = [error, self.response]
                self.response.__enter__.return_value = io.BytesIO(b'{"ok":true}')
                self.assertEqual(self.request(), {'ok': True})

    def test_exhaustion_and_sanitization(self):
        for error in (self.http(503), URLError(TimeoutError('SECRET'))):
            self.network.reset_mock()
            self.reset_pacing()
            self.network.side_effect = error
            try:
                self.request()
            except ProviderError as exc:
                details = safe_error_details(exc)
                self.assertEqual(details['attempts'], 4)
                self.assertTrue(details['retryable'])
                self.assertTrue(details['exhausted'])
                self.assertNotIn('SECRET', str(exc))
                self.assertNotIn('SECRET', traceback.format_exc())
            else:
                self.fail('Expected ProviderError')
            self.assertEqual(self.network.call_count, 4)
            self.assertEqual(self.sleep.call_args_list, [call(1), call(2), call(4)])

    def test_nonretryable_http(self):
        for status in (400, 401, 403, 404):
            with self.subTest(status=status):
                self.reset_pacing()
                self.network.reset_mock()
                self.network.side_effect = self.http(status)
                with self.assertRaises(ProviderError) as caught:
                    self.request()
                self.assertEqual(caught.exception.http_status, status)
                self.assertFalse(caught.exception.retryable)
                self.assertFalse(caught.exception.exhausted)
                self.assertEqual(self.network.call_count, 1)
        self.sleep.assert_not_called()

    def test_invalid_json_not_retried(self):
        self.network.return_value = self.response
        self.response.__enter__.return_value = io.BytesIO(b'SECRET invalid JSON')
        with self.assertRaises(ProviderError) as caught:
            self.request()
        self.assertEqual(caught.exception.category, 'invalid_json')
        self.assertEqual(self.network.call_count, 1)
        self.sleep.assert_not_called()

    def test_nontransient_url_error_not_retried(self):
        self.network.side_effect = URLError('SECRET certificate failure')
        with self.assertRaises(ProviderError) as caught:
            self.request()
        self.assertFalse(caught.exception.retryable)
        self.assertNotIn('SECRET', str(caught.exception))
        self.assertEqual(self.network.call_count, 1)
        self.sleep.assert_not_called()

    @patch('providers.time.time', return_value=0)
    def test_retry_after_seconds_and_date(self, clock):
        for value, delay in [('3', 15), ('Thu, 01 Jan 1970 00:00:05 GMT', 15),
                             ('60', 60), ('120', 120), ('SECRET invalid', 15), ('-5', 15)]:
            with self.subTest(value=value):
                self.reset_pacing()
                self.network.side_effect = [self.http(429, value), self.response]
                self.response.__enter__.return_value = io.BytesIO(b'{"ok":true}')
                self.request()
                self.sleep.assert_called_once_with(delay)

    @patch('providers.time.time', return_value=0)
    def test_long_retry_after_stops_without_retrying_early(self, clock):
        for value in ('181', 'Thu, 01 Jan 1970 00:03:01 GMT'):
            self.reset_pacing()
            self.network.reset_mock()
            self.network.side_effect = self.http(429, value)
            with self.assertRaises(ProviderError) as caught:
                self.request()
            self.assertEqual(caught.exception.retry_after, 181)
            self.assertTrue(caught.exception.exhausted)
            self.assertEqual(caught.exception.attempts, 1)
            self.sleep.assert_not_called()
            self.assertEqual(self.network.call_count, 1)

    def test_overflowing_retry_after_stops_with_finite_diagnostics(self):
        self.network.side_effect = self.http(429, '9' * 400)
        with self.assertRaises(ProviderError) as caught:
            self.request()
        details = safe_error_details(caught.exception)
        self.assertGreater(details['retry_after'], 60)
        self.assertEqual(details['attempts'], 1)
        self.assertTrue(details['retryable'])
        self.assertTrue(details['exhausted'])
        json.dumps(details, allow_nan=False)
        self.assertNotIn('Infinity', str(caught.exception))
        self.assertEqual(self.network.call_count, 1)
        self.sleep.assert_not_called()

    def test_final_429_retains_shared_cooldown_and_escalation(self):
        self.network.side_effect = self.http(429)
        with self.assertRaises(ProviderError) as caught:
            self.request()
        self.assertEqual(caught.exception.attempts, 4)
        self.assertEqual(self.sleep.call_args_list, [call(15), call(30), call(60)])
        self.network.side_effect = None
        self.network.return_value = self.response
        self.request()
        self.assertEqual(self.sleep.call_args_list[-1], call(60))

    def test_fresh_agent_and_judge_share_endpoint(self):
        with patch.dict('os.environ', {'GEMINI_API_KEY': 'SECRET'}):
            self.network.side_effect = lambda *a, **k: io.BytesIO(
                b'{"choices":[{"message":{"content":"ok"}}]}')
            for model in ('agent', 'judge', 'next-case'):
                providers.Provider('gemini', model).complete([], [])
        self.assertEqual(self.sleep.call_args_list, [call(1), call(1)])

    def test_separate_endpoints_do_not_share_cooldown(self):
        self.network.side_effect = self.http(429, '999')
        with self.assertRaises(ProviderError):
            self.request()
        self.network.side_effect = None
        self.network.return_value = self.response
        post_json('https://other.invalid', {}, {})
        self.sleep.assert_not_called()

    def test_budget_blocks_fresh_call_without_network_or_secret(self):
        self.network.side_effect = self.http(429, '9' * 400)
        with self.assertRaises(ProviderError):
            self.request()
        self.network.reset_mock()
        with self.assertRaises(ProviderError) as caught:
            self.request()
        self.assertEqual(caught.exception.category, 'rate_limit')
        self.assertEqual(caught.exception.attempts, 0)
        self.assertNotIn('SECRET', str(caught.exception))
        self.network.assert_not_called()
        self.sleep.assert_not_called()

    def test_cumulative_budget(self):
        with patch.dict('os.environ', {'PROVIDER_MAX_WAIT': '40'}):
            self.network.side_effect = self.http(429)
            with self.assertRaises(ProviderError) as caught:
                self.request()
        self.assertEqual(caught.exception.attempts, 2)
        self.sleep.assert_called_once_with(15)

    def test_nonadvancing_sleep_is_bounded_without_early_network(self):
        providers._ENDPOINTS['https://example.invalid'] = {
            'next': 0, 'cooldown': self.now + 1, 'strikes': 1}
        self.sleep.side_effect = None
        with patch.dict('os.environ', {'PROVIDER_MAX_WAIT': '2'}):
            with self.assertRaises(ProviderError) as caught:
                self.request()
        self.assertEqual(caught.exception.category, 'rate_limit')
        self.assertEqual(self.sleep.call_count, 2)
        self.network.assert_not_called()

    def test_wakeup_rechecks_new_cooldown_without_holding_lock(self):
        providers._ENDPOINTS['https://example.invalid'] = {
            'next': self.now + 1, 'cooldown': 0, 'strikes': 0}
        def sleep(seconds):
            self.assertTrue(providers._PACING_LOCK.acquire(blocking=False))
            try:
                self.advance(seconds)
                if self.sleep.call_count == 1:
                    providers._ENDPOINTS['https://example.invalid']['cooldown'] = self.now + 15
            finally:
                providers._PACING_LOCK.release()
        self.sleep.side_effect = sleep
        self.network.return_value = self.response
        self.request()
        self.assertEqual(self.sleep.call_args_list, [call(1), call(15)])

    def test_concurrent_calls_release_lock_during_network(self):
        entered, second = threading.Event(), threading.Event()
        times = []
        def network(*args, **kwargs):
            self.assertTrue(providers._PACING_LOCK.acquire(blocking=False))
            providers._PACING_LOCK.release()
            times.append(self.now)
            if len(times) == 1:
                entered.set()
                self.assertTrue(second.wait(timeout=2))
            else:
                second.set()
            return io.BytesIO(b'{"ok":true}')
        self.network.side_effect = network
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.request)
            self.assertTrue(entered.wait(timeout=2))
            other = pool.submit(self.request)
            self.assertEqual(other.result(timeout=2), {'ok': True})
            self.assertEqual(first.result(timeout=2), {'ok': True})
        self.assertEqual(times, [1000, 1001])

    def test_invalid_config_is_sanitized_before_network(self):
        for name in ('PROVIDER_MIN_INTERVAL', 'PROVIDER_RATE_LIMIT_COOLDOWN',
                     'PROVIDER_MAX_WAIT'):
            for value in ('SECRET', 'nan', 'inf', '-1', '99999'):
                with self.subTest(name=name, value=value), patch.dict('os.environ', {name: value}):
                    with self.assertRaises(ProviderError) as caught:
                        self.request()
                    self.assertEqual(caught.exception.category, 'invalid_config')
                    self.assertNotIn(value, str(caught.exception))
        self.network.assert_not_called()


class PropagationTests(unittest.TestCase):
    def setUp(self):
        self.error = ProviderError('rate_limit', 429, 4, True, True, 2)
        self.provider = Mock()
        self.provider.complete.side_effect = self.error
        self.case = {'id': 'X', 'question': 'q', 'expected': 'e'}
        self.args = SimpleNamespace(provider='openai', model='a',
                                    judge_provider='openai', judge_model='j', max_steps=8)

    def test_agent_trace_preserves_details_and_contract(self):
        result = run_agent('q', self.provider, Mock())
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['error'], 'PROVIDER_ERROR')
        self.assertEqual(result['trace'][0]['diagnostics'], safe_error_details(self.error))

    def test_judge_error_stays_unscored_with_details(self):
        grade = grade_case(self.case, {'status': 'completed'}, self.provider)
        self.assertIsNone(grade['score'])
        self.assertEqual(grade['diagnostics'], safe_error_details(self.error))

    def test_runner_failure_paths_are_safe(self):
        for error in (self.error, RuntimeError('SECRET key and response')):
            with self.subTest(error=type(error).__name__):
                with patch.object(run_evals, 'Provider', side_effect=error):
                    item = run_evals.run_case(self.case, self.args)
                self.assertIsNone(item['grade']['score'])
                for part in ('result', 'grade'):
                    self.assertEqual(item[part]['diagnostics'], safe_error_details(error))
                self.assertNotIn('SECRET', json.dumps(item))

    def test_generic_judge_and_agent_messages_are_not_reported(self):
        self.provider.complete.side_effect = RuntimeError('SECRET')
        result = run_agent('q', self.provider, Mock())
        grade = grade_case(self.case, {'status': 'completed'}, self.provider)
        self.assertNotIn('SECRET', json.dumps([result, grade]))


if __name__ == '__main__':
    unittest.main()
