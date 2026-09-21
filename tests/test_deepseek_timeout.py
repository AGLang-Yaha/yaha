#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

'''Regression tests for the DeepSeek streaming stall ("completely blocked").

The provider's OpenAI client was constructed with no timeout, so the SDK
default (a 600s read) applied: a stream that went silently dead mid-completion
froze the run for ten minutes per attempt, one frozen retry after another.
The fix pins an explicit httpx timeout (read=120s per chunk) on the client,
and a stalled stream must surface as a retriable error string, not a hang.
'''
import os
import types
import unittest
from unittest.mock import patch

import httpx

import yaha.llm.deepseek as ds


def _provider():
    cfg = types.SimpleNamespace(model='deepseek-v4', base_url=None, max_tokens=None)
    return ds.DeepSeekProvider(cfg)


def test_client_constructed_with_explicit_timeout():
    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'test-key'}), \
         patch.object(ds, 'OpenAI', FakeOpenAI):
        _provider()
    timeout = captured.get('timeout')
    assert timeout is not None, 'client must be constructed with an explicit timeout'
    assert isinstance(timeout, httpx.Timeout), timeout
    assert timeout.read == 120.0, timeout
    assert timeout.connect == 10.0, timeout
    assert captured.get('max_retries') == 0, captured
    print('test_client_constructed_with_explicit_timeout OK')


def test_stalled_stream_becomes_retriable_error_not_hang():
    class FakeStream:
        def __iter__(self):
            raise httpx.ReadTimeout('simulated stalled stream')

    class FakeCompletions:
        def create(self, **kwargs):
            return FakeStream()

    class FakeClient:
        class chat:
            completions = FakeCompletions()

    provider = _provider()
    provider.client = FakeClient()
    text, usage, gm = provider.generate([{'role': 'user', 'content': 'hi'}])
    assert text.startswith('Error calling DeepSeek:'), text
    assert 'stalled stream' in text, text
    assert usage == {} and gm is None
    print('test_stalled_stream_becomes_retriable_error_not_hang OK')


def test_missing_api_key_is_an_error_not_a_crash():
    env = {k: v for k, v in os.environ.items() if k != 'DEEPSEEK_API_KEY'}
    with patch.dict(os.environ, env, clear=True):
        provider = _provider()
    assert provider.client is None
    text, usage, gm = provider.generate([{'role': 'user', 'content': 'hi'}])
    assert 'DEEPSEEK_API_KEY' in text, text
    print('test_missing_api_key_is_an_error_not_a_crash OK')


class DeepSeekTimeoutTests(unittest.TestCase):
    '''Unittest-discoverable wrapper for the regression functions above.'''
    test_client_constructed_with_explicit_timeout = staticmethod(test_client_constructed_with_explicit_timeout)
    test_stalled_stream_becomes_retriable_error_not_hang = staticmethod(test_stalled_stream_becomes_retriable_error_not_hang)
    test_missing_api_key_is_an_error_not_a_crash = staticmethod(test_missing_api_key_is_an_error_not_a_crash)


if __name__ == '__main__':
    unittest.main()
