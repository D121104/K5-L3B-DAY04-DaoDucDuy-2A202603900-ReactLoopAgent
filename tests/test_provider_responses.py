"""Offline adapter envelope validation and lossless message handling."""
import json
from pathlib import Path
import sys
import traceback
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from providers import Provider, ProviderError, safe_error_details


class ResponseTests(unittest.TestCase):
    def provider(self, name):
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'SECRET_KEY',
                                       'ANTHROPIC_API_KEY': 'SECRET_KEY'}):
            return Provider(name, 'test-model')

    def assert_invalid(self, name, envelopes):
        provider = self.provider(name)
        for envelope in envelopes:
            with self.subTest(envelope=envelope), \
                    patch('providers.post_json', return_value=envelope) as post, \
                    patch('providers.time.sleep') as sleep:
                try:
                    provider.complete([], [])
                except ProviderError as exc:
                    details = safe_error_details(exc)
                    self.assertEqual(details, {
                        'category': 'invalid_response', 'http_status': None,
                        'attempts': 1, 'retryable': False, 'exhausted': False})
                    diagnostic = json.dumps(details, allow_nan=False)
                    for text in (diagnostic, str(exc), traceback.format_exc()):
                        self.assertNotIn('RESPONSE_SENTINEL', text)
                        self.assertNotIn('SECRET_KEY', text)
                else:
                    self.fail('Expected invalid_response')
                post.assert_called_once()
                sleep.assert_not_called()

    def test_compatible_invalid_envelopes(self):
        envelopes = [None, [], 'RESPONSE_SENTINEL', {}, {'choices': None},
                     {'choices': []}, {'choices': {}}, {'choices': [None]},
                     {'choices': [{'message': 'RESPONSE_SENTINEL'}]}]
        for message in ({}, {'content': 42}, {'tool_calls': {}},
                        {'tool_calls': ['RESPONSE_SENTINEL']},
                        {'tool_calls': [{'id': 'RESPONSE_SENTINEL'}]},
                        {'tool_calls': [{'id': 'x', 'function': {'name': 'f'}}]},
                        {'tool_calls': [{'id': 'x', 'function': {
                            'name': 'f', 'arguments': {}}}]}):
            envelopes.append({'choices': [{'message': message}]})
        self.assert_invalid('openai', envelopes)

    def test_anthropic_invalid_envelopes(self):
        self.assert_invalid('anthropic', [None, [], 'RESPONSE_SENTINEL', {},
            {'content': None}, {'content': 'RESPONSE_SENTINEL'},
            {'content': [None]}, {'content': [{}]},
            {'content': [{'type': 'text', 'text': 42}]},
            {'content': [{'type': 'tool_use', 'id': 'RESPONSE_SENTINEL'}]},
            {'content': [{'type': 'tool_use', 'id': 'x', 'name': 'f',
                          'input': 'RESPONSE_SENTINEL'}]}])

    def test_compatible_message_is_preserved(self):
        message = {'role': 'assistant', 'content': None, 'extra': {'opaque': True},
                   'tool_calls': [{'id': 'c', 'type': 'function', 'extra': 1,
                       'function': {'name': 'lookup', 'arguments': '{"x":1}'}}]}
        with patch('providers.post_json', return_value={
                'choices': [{'message': message, 'finish_reason': 'tool_calls'}]}):
            result = self.provider('openai').complete([], [])
        self.assertIs(result['message'], message)
        self.assertEqual(result['calls'], [
            {'id': 'c', 'name': 'lookup', 'arguments': '{"x":1}'}])

    def test_compatible_content_variants(self):
        for message in ({'content': ''}, {'content': None, 'tool_calls': None},
                        {'content': [{'type': 'text', 'text': 'hi'}]}):
            with self.subTest(message=message), patch('providers.post_json',
                    return_value={'choices': [{'message': message}]}):
                self.assertIs(self.provider('openai').complete([], [])['message'], message)

    def test_anthropic_tool_normalization_and_unknown_blocks(self):
        envelope = {'content': [{'type': 'thinking', 'thinking': 'opaque'},
                    {'type': 'tool_use', 'id': 'c', 'name': 'lookup',
                     'input': {'x': 1}}], 'stop_reason': 'tool_use'}
        with patch('providers.post_json', return_value=envelope):
            result = self.provider('anthropic').complete([], [])
        self.assertIsNone(result['message']['content'])
        self.assertEqual(result['finish'], 'tool_calls')
        self.assertEqual(json.loads(result['calls'][0]['arguments']), {'x': 1})
        self.assertEqual(result['message']['tool_calls'][0]['function']['name'], 'lookup')


if __name__ == '__main__':
    unittest.main()
