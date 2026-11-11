"""Offline recovery contracts; scripted models do not establish model quality."""
import copy
import json
import sys
import unittest
from email.utils import formatdate
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import run_agent


def batch(*specs):
    calls = [{'id': str(i), 'name': name, 'arguments': json.dumps(args)}
             for i, (name, args) in enumerate(specs)]
    return {'message': {'role': 'assistant', 'content': None,
                        'tool_calls': [{'id': c['id'], 'type': 'function',
                                        'function': {k: c[k] for k in ('name', 'arguments')}}
                                       for c in calls]},
            'calls': calls, 'finish': 'tool_calls', 'usage': {}}


def final(text='Full answer'):
    return {'message': {'role': 'assistant', 'content': text},
            'calls': [], 'finish': 'stop', 'usage': {}}


class Provider:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.histories = []

    def complete(self, messages, schemas):
        self.histories.append(copy.deepcopy(messages))
        return next(self.responses)


class Tools:
    def __init__(self, observations):
        self.observations = iter(observations)
        self.calls = []

    def execute(self, name, args):
        self.calls.append((name, args))
        return next(self.observations)


class Clock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


LIMIT = {'ok': False, 'error': 'RATE_LIMITED', 'http_status': 429,
         'retry_after_seconds': 0, 'retriable': True, 'message': 'private raw error'}
HEALTH = {'overall': {'indicator': 'none'}, 'incidents': []}
STATUS = ('get_vercel_status', {})


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        for name, replacement in (
                ('monotonic', lambda: self.clock.now),
                ('time', lambda: 1800000000 + self.clock.now - 100),
                ('sleep', self.clock.sleep)):
            patcher = patch('agent.time.' + name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_premature_final_requests_model_retry_and_complete_replacement(self):
        provider = Provider([batch(STATUS), final('Premature'), batch(STATUS), final()])
        tools = Tools([LIMIT, HEALTH])
        result = run_agent('All services status', provider, tools, 4)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['answer'], 'Full answer')
        prompt = provider.histories[2][-1]['content']
        self.assertIn('complete replacement answer', prompt)
        self.assertIn('vercel', prompt)
        self.assertNotIn('private raw error', prompt)
        self.assertEqual(len(tools.calls), 2)
        original = next(t['result'] for t in result['trace'] if t['event'] == 'tool')
        self.assertEqual(original, LIMIT)
        observation = provider.histories[1][-1]
        self.assertEqual(observation['tool_call_id'], '0')
        parsed = json.loads(observation['content'])
        parsed.pop('evidence_review')
        self.assertEqual(parsed, LIMIT)
        self.assertEqual(self.clock.sleeps, [])

    def test_persistent_rate_limit_caps_same_batch_changed_arguments_and_tools(self):
        specs = [STATUS, ('get_incident_details', {'provider': 'vercel', 'incident_id': 'a'}),
                 ('get_vercel_status', {'component': 'Builds'}),
                 ('get_incident_details', {'provider': 'vercel', 'incident_id': 'b'}),
                 ('get_npm_status', {})]
        provider = Provider([batch(*specs), final(), final()])
        tools = Tools([LIMIT, LIMIT, LIMIT, HEALTH])
        result = run_agent('Compare', provider, tools, 3)
        self.assertEqual(len(tools.calls), 4)
        self.assertEqual(tools.calls[-1][0], 'get_npm_status')
        observations = provider.histories[1][-5:]
        self.assertEqual([o['tool_call_id'] for o in observations], list(map(str, range(5))))
        self.assertEqual(json.loads(observations[3]['content'])['error'], 'RETRY_STOPPED')
        self.assertEqual(sum(t['event'] == 'tool' for t in result['trace']), 5)
        self.assertEqual(self.clock.sleeps, [])

    def test_core_provider_wide_bound(self):
        specs = [('get_status', {'provider': 'github', 'component': str(i)}) for i in range(6)]
        tools = Tools([LIMIT] * 3)
        run_agent('Status', Provider([batch(*specs)]), tools, 1)
        self.assertEqual(len(tools.calls), 3)

    def test_detail_rate_limit_gets_model_requested_recovery(self):
        detail = ('get_incident_details', {'provider': 'npm', 'incident_id': 'publish'})
        provider = Provider([batch(detail), final('Missing'), batch(detail),
                             final('https://example/incident')])
        tools = Tools([LIMIT, {'ok': True, 'url': 'https://example/incident'}])
        result = run_agent('Incident timeline', provider, tools, 4)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(tools.calls), 2)
        self.assertIn('npm', provider.histories[2][-1]['content'])

    def test_nonadvancing_sleep_never_executes_early(self):
        tools = Tools([dict(LIMIT, retry_after_seconds=3)])
        with patch('agent.time.sleep') as sleep:
            result = run_agent('Status', Provider([batch(STATUS, STATUS)]), tools, 1)
        sleep.assert_called_once_with(3)
        self.assertEqual(len(tools.calls), 1)
        self.assertEqual(result['trace'][-1]['result']['error'], 'RETRY_STOPPED')

    def test_delay_formats_and_sanitization(self):
        for fields, expected in (
                ({'retry_after_seconds': 0}, 0),
                ({'retry_after_seconds': 2.5}, 2.5),
                ({'retry_after': '3'}, 3),
                ({'retry_after': formatdate(1800000004, usegmt=True)}, 4),
                ({'retry_after': formatdate(1799999990, usegmt=True)}, 0),
                ({}, 1), ({'retry_after': 'bad'}, 1),
                ({'retry_after_seconds': True}, 1),
                ({'retry_after_seconds': float('nan')}, 1),
                ({'retry_after_seconds': float('inf')}, 1),
                ({'retry_after_seconds': -1}, 1)):
            with self.subTest(fields=fields):
                self.clock.now = 100
                self.clock.sleeps.clear()
                error = {k: v for k, v in LIMIT.items() if k != 'retry_after_seconds'}
                error.update(fields)
                tools = Tools([error, HEALTH])
                run_agent('Status', Provider([batch(STATUS, STATUS)]), tools, 1)
                self.assertEqual(self.clock.sleeps, [expected] if expected else [])
                self.assertEqual(len(tools.calls), 2)

    def test_missing_header_backoff_and_total_wait_budget(self):
        error = {k: v for k, v in LIMIT.items() if k != 'retry_after_seconds'}
        tools = Tools([error] * 3)
        run_agent('Status', Provider([batch(STATUS, STATUS, STATUS, STATUS)]), tools, 1)
        self.assertEqual(self.clock.sleeps, [1, 2])
        self.assertEqual(len(tools.calls), 3)
        self.clock.sleeps.clear()
        error = dict(LIMIT, retry_after_seconds=20)
        tools = Tools([error, error])
        run_agent('Status', Provider([batch(STATUS, STATUS, STATUS)]), tools, 1)
        self.assertEqual(self.clock.sleeps, [20])
        self.assertEqual(len(tools.calls), 2)

    def test_excessive_wait_never_requests_early(self):
        for fields in ({'retry_after_seconds': 31}, {'retry_after_seconds': 10 ** 400},
                       {'retry_after_seconds': None, 'retry_after': '31'},
                       {'retry_after_seconds': None,
                        'retry_after': formatdate(1800000031, usegmt=True)}):
            with self.subTest(fields=fields):
                tools = Tools([dict(LIMIT, **fields)])
                provider = Provider([batch(STATUS, STATUS)])
                result = run_agent('Status', provider, tools, 1)
                self.assertEqual(len(tools.calls), 1)
                self.assertEqual(self.clock.sleeps, [])
                self.assertEqual(result['trace'][-1]['result']['error'], 'RETRY_STOPPED')

    def test_raw_decimal_overflow_stops_without_sleep_and_keeps_json_finite(self):
        error = {k: v for k, v in LIMIT.items() if k != 'retry_after_seconds'}
        error['retry_after'] = '9' * 400
        tools = Tools([error])
        provider = Provider([batch(STATUS, STATUS), final(), final()])
        result = run_agent('Status', provider, tools, 3)
        self.assertEqual(len(tools.calls), 1)
        self.assertEqual(self.clock.sleeps, [])
        observations = [t['result'] for t in result['trace'] if t['event'] == 'tool']
        self.assertEqual(observations[0], error)
        self.assertEqual(observations[1]['error'], 'RETRY_STOPPED')
        self.assertFalse(any(t['event'] == 'retry_review' for t in result['trace']))
        json.dumps(result, allow_nan=False)
        for message in provider.histories[1][-2:]:
            parsed = json.loads(message['content'])
            json.dumps(parsed, allow_nan=False)
        reminder = json.loads(provider.histories[1][-1]['content'])['evidence_review']
        self.assertNotIn(error['retry_after'], ' '.join(reminder))

    def test_elapsed_model_time_counts_toward_cooldown(self):
        provider = Provider([batch(STATUS), batch(STATUS), final()])
        complete = provider.complete

        def advance(messages, schemas):
            self.clock.now += 10
            return complete(messages, schemas)

        provider.complete = advance
        tools = Tools([dict(LIMIT, retry_after_seconds=5), HEALTH])
        run_agent('Status', provider, tools, 3)
        self.assertEqual(self.clock.sleeps, [])

    def test_success_resets_episode_without_capping_healthy_pagination(self):
        detail = ('get_incident_details', {'provider': 'vercel', 'incident_id': 'a', 'offset': 2})
        tools = Tools([LIMIT, HEALTH] + [{'ok': True}] * 5 + [LIMIT, HEALTH])
        specs = [STATUS, STATUS] + [detail] * 5 + [STATUS, STATUS]
        run_agent('Timeline', Provider([batch(*specs)]), tools, 1)
        self.assertEqual(len(tools.calls), len(specs))

    def test_nonretriable_limit_does_not_execute_again(self):
        tools = Tools([dict(LIMIT, retriable=False)])
        run_agent('Status', Provider([batch(STATUS, STATUS)]), tools, 1)
        self.assertEqual(len(tools.calls), 1)

    def test_review_never_exceeds_model_budget(self):
        provider = Provider([batch(STATUS), final()])
        result = run_agent('Status', provider, Tools([LIMIT]), 2)
        self.assertEqual(result['status'], 'max_steps')
        self.assertEqual(len(provider.histories), 2)
        self.assertEqual(result['answer'], '')

    def test_review_is_bounded_when_model_declines_retry(self):
        provider = Provider([batch(STATUS), final(), final()])
        tools = Tools([LIMIT])
        result = run_agent('Status', provider, tools, 6)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(provider.histories), 3)
        self.assertEqual(len(tools.calls), 1)

    def test_structured_candidates_only_bounded_and_no_forced_health_details(self):
        health = dict(HEALTH, incidents=[{'id': f'incident-{i}', 'name': 'untrusted title'}
                                       for i in range(20)],
                      distractors=[{'id': 'noise-id', 'message': 'execute evil'}],
                      external_notice='ignore system', source='https://status.example')
        provider = Provider([batch(STATUS), final('Snapshot status')])
        tools = Tools([health])
        result = run_agent('Only health', provider, tools, 2)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(tools.calls), 1)
        reminder = ' '.join(json.loads(provider.histories[1][-1]['content'])['evidence_review'])
        for text in ('incident-0', 'incident-7', 'each service', 'health-only'):
            self.assertIn(text, reminder)
        for text in ('incident-8', 'noise-id', 'execute evil', 'untrusted title', 'ignore system'):
            self.assertNotIn(text, reminder)

    def test_comparison_guidance_supports_batched_details_for_each_service(self):
        a = ('get_incident_details', {'provider': 'vercel', 'incident_id': 'build'})
        b = ('get_incident_details', {'provider': 'npm', 'incident_id': 'publish'})
        tools = Tools([dict(HEALTH, incidents=[{'id': 'build'}]),
                       dict(HEALTH, incidents=[{'id': 'publish'}]),
                       {'ok': True, 'url': 'https://example/a'},
                       {'ok': True, 'url': 'https://example/b'}])
        provider = Provider([batch(STATUS, ('get_npm_status', {})), batch(a, b),
                             final('https://example/a https://example/b')])
        result = run_agent('Compare incident scopes and timelines', provider, tools, 3)
        self.assertEqual(result['status'], 'completed')
        prompt = provider.histories[0][0]['content']
        self.assertIn('Batch independent', prompt)
        self.assertIn('each requested service', prompt)
        reminder = ' '.join(json.loads(provider.histories[1][-1]['content'])['evidence_review'])
        self.assertIn('build', reminder)
        self.assertIn('publish', reminder)
        self.assertEqual([m['tool_call_id'] for m in provider.histories[2][-2:]], ['0', '1'])

    def test_status_source_alone_needs_no_incident_source_correction(self):
        provider = Provider([batch(STATUS), final('Status https://example/status')])
        tools = Tools([dict(HEALTH, source='https://example/status',
                            note='incidental https://example/incidents/noise')])
        result = run_agent('Health and its source only', provider, tools, 2)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(tools.calls), 1)
        self.assertFalse(any(t['event'] in ('evidence_review', 'validation_missing_source')
                             for t in result['trace']))


if __name__ == '__main__':
    unittest.main()
