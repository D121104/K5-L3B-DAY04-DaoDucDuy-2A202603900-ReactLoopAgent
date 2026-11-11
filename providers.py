"""Bốn provider cùng một interface. Không chứa agent loop."""
import json
import os
import errno
import math
import socket
import time
import threading
from email.utils import parsedate_to_datetime
from datetime import timezone
import urllib.error
import urllib.request

CONFIG = {
    'openai': ('OPENAI_API_KEY', 'https://api.openai.com/v1/chat/completions'),
    'gemini': ('GEMINI_API_KEY', 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'),
    'openrouter': ('OPENROUTER_API_KEY', 'https://openrouter.ai/api/v1/chat/completions'),
    'anthropic': ('ANTHROPIC_API_KEY', 'https://api.anthropic.com/v1/messages'),
    'groq': ('GROQ_API_KEY', 'https://api.groq.com/openai/v1/chat/completions'),
}

_PACING_LOCK = threading.Lock()
_ENDPOINTS = {}


def _transport_config():
    values = []
    for name, default, minimum, maximum in (
            ('PROVIDER_MIN_INTERVAL', 1, 0, 60),
            ('PROVIDER_RATE_LIMIT_COOLDOWN', 15, 1, 60),
            ('PROVIDER_MAX_WAIT', 180, 0, 3600)):
        try:
            value = float(os.environ.get(name, default))
            if not math.isfinite(value) or not minimum <= value <= maximum:
                raise ValueError
        except (ValueError, TypeError):
            raise ProviderError('invalid_config', attempts=0) from None
        values.append(value)
    return values


class ProviderError(RuntimeError):
    """Provider failure containing only report-safe diagnostic fields."""

    def __init__(self, category, http_status=None, attempts=1, retryable=False,
                 exhausted=False, retry_after=None):
        self.category = category
        self.http_status = http_status
        self.attempts = attempts
        self.retryable = retryable
        self.exhausted = exhausted
        self.retry_after = retry_after
        super().__init__(json.dumps(safe_error_details(self), sort_keys=True))


def safe_error_details(exc):
    """Never serialize exception messages, response bodies, URLs or reasons."""
    if isinstance(exc, ProviderError):
        details = {key: getattr(exc, key) for key in
                   ('category', 'http_status', 'attempts', 'retryable', 'exhausted')}
        if exc.retry_after is not None:
            details['retry_after'] = exc.retry_after
        return details
    return {'category': 'unexpected_error', 'http_status': None, 'attempts': 0,
            'retryable': False, 'exhausted': False}


def _retry_after(value):
    if not value:
        return None
    try:
        seconds = float(value)
        if seconds == math.inf:
            # Overflow is an excessive wait, not a missing header. Use a finite
            # over-budget sentinel so diagnostics remain valid JSON.
            return float('1e100')
    except (ValueError, TypeError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = date.timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0, seconds) if math.isfinite(seconds) else None


def post_json(url, headers, payload):
    interval, cooldown_base, budget = _transport_config()
    waited, attempts, ready = 0, 0, 0
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json', 'Accept': 'application/json', 'User-Agent': 'ReactLoopAgent/1.0', **headers})
    for attempt in range(1, 5):
        # Reserve an attempt atomically, but never hold the lock while sleeping
        # or doing I/O. A waking caller must observe any newer shared cooldown.
        while True:
            with _PACING_LOCK:
                state = _ENDPOINTS.setdefault(url, {'next': 0, 'cooldown': 0, 'strikes': 0})
                now = time.monotonic()
                delay = max(0, state['next'] - now, state['cooldown'] - now, ready - now)
                limited = state['cooldown'] > now
                if delay == 0:
                    state['next'] = now + interval
                    break
            if delay > budget - waited or waited >= budget:
                raise ProviderError('rate_limit' if limited else 'wait_budget',
                                    429 if limited else None, attempts, True, True,
                                    delay) from None
            time.sleep(delay)
            waited += delay
        attempts = attempt
        status, retry_after = None, None
        try:
            with urllib.request.urlopen(req, timeout=60) as response:
                data = json.load(response)
            with _PACING_LOCK:
                _ENDPOINTS[url]['strikes'] = 0
            return data
        except urllib.error.HTTPError as exc:
            status = exc.code
            retry_after = _retry_after(exc.headers.get('Retry-After')) if exc.headers else None
            retryable = status in {429, 500, 502, 503, 504}
            category = 'rate_limit' if status == 429 else 'http_error'
            exc.close()
        except (urllib.error.URLError, OSError) as exc:
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            retryable = (isinstance(reason, (TimeoutError, ConnectionError))
                         or isinstance(reason, OSError) and reason.errno in {
                             errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNREFUSED,
                             errno.ECONNABORTED, errno.EPIPE, errno.ENETUNREACH,
                             errno.EHOSTUNREACH, socket.EAI_AGAIN})
            category = 'timeout' if isinstance(reason, TimeoutError) else 'connection_error'
        except (ValueError, UnicodeError):
            raise ProviderError('invalid_json', attempts=attempt) from None

        delay = max(2 ** (attempt - 1), retry_after or 0)
        if status == 429:
            with _PACING_LOCK:
                state = _ENDPOINTS[url]
                state['strikes'] = min(state['strikes'] + 1, 7)
                delay = max(delay, min(60, cooldown_base * 2 ** (state['strikes'] - 1)))
                state['cooldown'] = max(state['cooldown'], time.monotonic() + delay)
        if not retryable or attempt == 4 or delay > budget - waited:
            raise ProviderError(category, status, attempt, retryable,
                                retryable and (attempt == 4 or delay > budget - waited),
                                retry_after) from None
        ready = time.monotonic() + delay


class Provider:
    """complete(messages, schemas) -> {message, calls, finish, usage}.

    messages: system/user/assistant/tool theo format Chat Completions. message trả về
    cần được append nguyên vẹn; tool result có role, tool_call_id, content (JSON string).
    """
    def __init__(self, name, model):
        if name not in CONFIG:
            raise ValueError(f'Provider không hợp lệ: {name}')
        if not model:
            raise ValueError('Cần đặt MODEL là model có hỗ trợ tool calling.')
        self.name, self.model = name, model
        key_name, self.url = CONFIG[name]
        self.key = os.getenv(key_name)
        if not self.key:
            raise ValueError(f'Thiếu {key_name}; xem README.md.')

    def complete(self, messages, schemas):
        if self.name == 'anthropic':
            return self._anthropic(messages, schemas)
        payload = {'model': self.model, 'messages': messages}
        if schemas:
            payload['tools'] = schemas
        data = post_json(self.url, {'Authorization': f'Bearer {self.key}'}, payload)
        if (not isinstance(data, dict) or not isinstance(data.get('choices'), list)
                or not data['choices'] or not isinstance(data['choices'][0], dict)
                or not isinstance(data['choices'][0].get('message'), dict)):
            raise ProviderError('invalid_response') from None
        choice = data['choices'][0]
        message = choice['message']
        tool_calls = message.get('tool_calls')
        if tool_calls is None:
            tool_calls = []
        if (not isinstance(tool_calls, list)
                or any(not isinstance(c, dict) or not isinstance(c.get('id'), str)
                       or not isinstance(c.get('function'), dict)
                       or not isinstance(c['function'].get('name'), str)
                       or not isinstance(c['function'].get('arguments'), str)
                       for c in tool_calls)
                or ('content' not in message and not tool_calls)
                or (message.get('content') is not None
                    and not isinstance(message['content'], (str, list)))):
            raise ProviderError('invalid_response') from None
        calls = [{'id': c['id'], 'name': c['function']['name'], 'arguments': c['function']['arguments']} for c in tool_calls]
        return {'message': message, 'calls': calls, 'finish': choice.get('finish_reason'), 'usage': data.get('usage', {})}

    def _anthropic(self, messages, schemas):
        system = '\n'.join(m['content'] for m in messages if m['role'] == 'system')
        native = []
        for m in messages:
            if m['role'] == 'system':
                continue
            if m['role'] == 'assistant':
                blocks = []
                if m.get('content'):
                    blocks.append({'type': 'text', 'text': m['content']})
                for call in m.get('tool_calls', []):
                    blocks.append({'type': 'tool_use', 'id': call['id'], 'name': call['function']['name'], 'input': json.loads(call['function']['arguments'])})
                native.append({'role': 'assistant', 'content': blocks})
            elif m['role'] == 'tool':
                block = {'type': 'tool_result', 'tool_use_id': m['tool_call_id'], 'content': m['content']}
                if native and native[-1]['role'] == 'user' and isinstance(native[-1]['content'], list):
                    native[-1]['content'].append(block)
                else:
                    native.append({'role': 'user', 'content': [block]})
            else:
                native.append({'role': 'user', 'content': m['content']})
        tools = [{'name': x['function']['name'], 'description': x['function']['description'], 'input_schema': x['function']['parameters']} for x in schemas]
        payload = {'model': self.model, 'max_tokens': 1024, 'system': system, 'messages': native}
        if tools:
            payload['tools'] = tools
        data = post_json(self.url, {'x-api-key': self.key, 'anthropic-version': '2023-06-01'}, payload)
        if not isinstance(data, dict) or not isinstance(data.get('content'), list):
            raise ProviderError('invalid_response') from None
        blocks = data['content']
        if any(not isinstance(b, dict) or not isinstance(b.get('type'), str)
               or (b['type'] == 'text' and not isinstance(b.get('text'), str))
               or (b['type'] == 'tool_use'
                   and (not isinstance(b.get('id'), str)
                        or not isinstance(b.get('name'), str)
                        or not isinstance(b.get('input'), dict))) for b in blocks):
            raise ProviderError('invalid_response') from None
        calls = [{'id': b['id'], 'name': b['name'], 'arguments': json.dumps(b['input'], ensure_ascii=False)} for b in blocks if b['type'] == 'tool_use']
        text = '\n'.join(b['text'] for b in blocks if b['type'] == 'text')
        message = {'role': 'assistant', 'content': text or None}
        if calls:
            message['tool_calls'] = [{'id': c['id'], 'type': 'function', 'function': {'name': c['name'], 'arguments': c['arguments']}} for c in calls]
        reason = {'end_turn': 'stop', 'tool_use': 'tool_calls', 'max_tokens': 'length'}.get(data.get('stop_reason'), data.get('stop_reason'))
        return {'message': message, 'calls': calls, 'finish': reason, 'usage': data.get('usage', {})}
