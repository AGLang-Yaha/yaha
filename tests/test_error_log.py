#!/usr/bin/env python3
'''Tests for the silent recovered-error log.

The agent records errors it silently recovers from (API retries) into
.yaha/logs/... under the workspace: append-only, one greppable line per failed
attempt, never raising, and invisible to the operator.

Built like tests/test_api_retry_user_wait.py: a real Agent over a tmp YAHAEngine
workspace plus a scripted LLM, so the logger, the session and the on-disk path
are the real ones and no TTY is ever needed.
'''
import io
import re
import shutil
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from rich.console import Console

from yaha.core.agent import Agent
from yaha.core.engine import YAHAEngine


_AGENT_FIXTURE = '''
## ErrorLogNoTool
- **Inherited:** None
- **Code:** ELN
- **Toolset:**
> preamble
'''

_STAGE = {
    'name': 'Preamble',
    'allowed_tools': [],
    'exit_tools': [],
    'required_tools': [],
}

_ERR = 'Error calling MockProvider: 500 boom'
_CLOSE_TAG = '<' + '/tool_code>'
_MALFORMED = 'I tried to call a tool ' + _CLOSE_TAG


class _ScriptLLM:
    '''Scripted mock: next response each call, repeating the last one.'''

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.usage = {'prompt_tokens': 5, 'completion_tokens': 5}

    def generate(self, messages, **kwargs):
        idx = min(self.calls, len(self.responses) - 1)
        self.calls += 1
        return self.responses[idx], dict(self.usage), None


def _make_agent(responses, answers=None, unattended=True, retry_limit=3):
    '''Build a no-tool Agent over a tmp workspace with a scripted LLM.'''
    ws = Path(tempfile.mkdtemp(prefix='yaha_errorlog_'))
    (ws / '.yaha').mkdir(parents=True)
    for sub in ('memory', 'ext', 'logs'):
        (ws / '.yaha' / sub).mkdir()
    (ws / '.yaha' / 'tool-schema.md').write_text('[]')
    (ws / '.yaha' / 'AgentClass.md').write_text(_AGENT_FIXTURE)
    (ws / '.yaha' / 'Model.md').write_text('')
    eng = YAHAEngine(workspace_dir=str(ws))
    cfg = eng.agent_classes['ErrorLogNoTool']
    agent = Agent(
        agent_config=cfg,
        session=eng.session,
        models_config=eng.models_config,
        agent_classes=eng.agent_classes,
        tool_schemas=eng.tool_schemas,
        access_controller=eng.access_controller,
    )
    agent.llm_retry_limit = retry_limit
    agent.api_retry_prompt_timeout = 0.001
    agent.api_retry_wait_seconds = 0.001
    agent.api_retry_unattended_max_rounds = 2
    agent.unattended = unattended
    agent.ui.console = Console(width=100, file=io.StringIO())
    agent.llm = _ScriptLLM(responses)
    if answers is not None:
        box = list(answers)
        agent.ui.prompt_user = lambda *a, **k: box.pop(0) if box else None
    return agent, ws


def _force_toolset(agent, tools=('list_files',)):
    '''Give the agent a toolset so the validate_response branch is reachable.'''
    try:
        agent.config.toolset = list(tools)
    except Exception:
        object.__setattr__(agent.config, 'toolset', list(tools))
    assert agent.config.toolset, 'toolset override failed'


def _run(agent):
    return agent._run_react_loop(1, _STAGE)


def _entries(agent):
    path = Path(agent.session.get_error_log_path())
    if not path.exists():
        return []
    return [ln for ln in path.read_text(encoding='utf-8').splitlines() if ln.strip()]


def test_error_log_written_on_api_failure():
    agent, ws = _make_agent([_ERR] * 5, answers=['break'])
    try:
        with patch('time.sleep'):
            result, status, _ = _run(agent)
        assert status == 'ERROR', (result, status)
        lines = _entries(agent)
        assert len(lines) == 3, lines
        assert 'category=api' in lines[0], lines[0]
        assert 'agent=ErrorLogNoTool(' in lines[0], lines[0]
        assert 'stage=Preamble' in lines[0], lines[0]
        assert 'turn=1' in lines[0], lines[0]
        assert 'attempt=1' in lines[0], lines[0]
        assert _ERR in lines[0], lines[0]
        assert lines[0].startswith('20'), lines[0]
    finally:
        shutil.rmtree(ws, ignore_errors=True)

def test_every_failed_attempt_is_logged():
    agent, ws = _make_agent([_ERR, _ERR, 'All done.'], answers=['break'])
    try:
        with patch('time.sleep'):
            result, status, _ = _run(agent)
        assert status == 'DONE', (result, status)
        assert result == 'All done.', result
        lines = _entries(agent)
        assert len(lines) == 2, lines
        assert 'attempt=1' in lines[0], lines[0]
        assert 'attempt=2' in lines[1], lines[1]
        assert all('category=api' in ln for ln in lines), lines
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_validation_failure_is_logged():
    agent, ws = _make_agent([_MALFORMED] * 5, answers=['break'], retry_limit=1)
    try:
        _force_toolset(agent)
        with patch('time.sleep'):
            _run(agent)
        lines = _entries(agent)
        assert len(lines) == 1, lines
        assert 'category=api.validation' in lines[0], lines[0]
        assert 'Malformed tool call' in lines[0], lines[0]
        assert 'attempt=1' in lines[0], lines[0]
        # The validation path must carry the same context as the provider path.
        assert 'model=' in lines[0], lines[0]
        assert 'provider=ollama' in lines[0], lines[0]
    finally:
        shutil.rmtree(ws, ignore_errors=True)

def test_error_log_is_workspace_anchored():
    agent, ws = _make_agent([_ERR] * 5, answers=['break'], retry_limit=1)
    try:
        cwd_before = Path.cwd()
        with patch('time.sleep'):
            _run(agent)
        path = Path(agent.session.get_error_log_path())
        assert path.exists(), path
        assert str(path).startswith(str(ws)), (path, ws)
        rel = Path('.yaha') / 'logs' / agent.session.folder_name / 'error.log'
        assert not (cwd_before / rel).exists(), (cwd_before / rel)
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_log_error_never_raises():
    agent, ws = _make_agent([_ERR], answers=['break'])
    try:
        with patch('os.makedirs', side_effect=OSError('boom')):
            agent.log_error('api', 'something failed')
            agent.logger.log_error('api', 'something failed')
        assert _entries(agent) == []
    finally:
        shutil.rmtree(ws, ignore_errors=True)

def test_agent_wrapper_tolerates_missing_logger_method():
    agent, ws = _make_agent([_ERR], answers=['break'])
    try:
        original = agent.logger
        try:
            agent.logger = object()
            agent.log_error('api', 'ignored')
            agent.logger = None
            agent.log_error('api', 'ignored')
        finally:
            agent.logger = original
        assert _entries(agent) == []
    finally:
        shutil.rmtree(ws, ignore_errors=True)

def test_entry_is_single_line_and_truncated():
    agent, ws = _make_agent([_ERR], answers=['break'])
    try:
        huge = 'line one' + chr(10) + 'line two' + ('x' * 10000)
        agent.logger.log_error('api', huge, stage='Preamble', turn=1, attempt=1)
        lines = _entries(agent)
        assert len(lines) == 1, len(lines)
        assert chr(10) not in lines[0], lines[0]
        assert 'line one line two' in lines[0], lines[0]
        assert lines[0].endswith('[truncated]'), lines[0]
    finally:
        shutil.rmtree(ws, ignore_errors=True)


_FIELD_SCHEMA = ['category', 'agent', 'stage', 'turn', 'attempt', 'model', 'provider', 'msg']


def _field_names(line):
    '''Ordered key labels of a log line (timestamp and 'error' marker are bare).'''
    return [field.split('=', 1)[0] for field in line.split(' | ')[2:]]


def test_entry_carries_model_and_provider():
    agent, ws = _make_agent([_ERR] * 5, answers=['break'], retry_limit=1)
    try:
        with patch('time.sleep'):
            _run(agent)
        lines = _entries(agent)
        assert lines, 'no error entry written'
        line = lines[0]
        assert 'provider=ollama' in line, line
        expected_model = agent.config.model or '-'
        assert ('model=' + str(expected_model)) in line, line

        # A caller-supplied extra wins over the auto-attached values.
        agent.log_error(
            'api', 'boom',
            extra={'model': 'custom-model', 'provider': 'custom-provider'},
        )
        caller_line = _entries(agent)[-1]
        assert 'model=custom-model' in caller_line, caller_line
        assert 'provider=custom-provider' in caller_line, caller_line
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_pipe_in_message_is_escaped():
    agent, ws = _make_agent([_ERR], answers=['break'])
    try:
        raw = 'boom | msg=forged | attempt=999'
        agent.logger.log_error('api', raw, stage='Preamble', turn=1, attempt=1)
        lines = _entries(agent)
        assert len(lines) == 1, lines
        line = lines[0]
        escaped = raw.replace('|', chr(92) + '|')
        assert escaped in line, line
        assert ' | msg=forged' not in line, line
        # No forged fields: the delimiter count still matches the real schema.
        assert len(line.split(' | ')) == 8, line.split(' | ')
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def test_entry_field_order_is_stable():
    agent, ws = _make_agent([_ERR], answers=['break'])
    try:
        agent.log_error('api', 'boom', stage='Preamble', turn=2, attempt=1)
        lines = _entries(agent)
        assert len(lines) == 1, lines
        line = lines[0]
        head = line.split(' | ')
        stamp = r'^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$'
        assert re.match(stamp, head[0]), head[0]
        assert head[1] == 'error', head[1]
        assert _field_names(line) == _FIELD_SCHEMA, _field_names(line)
    finally:
        shutil.rmtree(ws, ignore_errors=True)


class ErrorLogTests(unittest.TestCase):
    '''Recovered-error-log functions exposed to unittest discovery.'''
    test_error_log_written_on_api_failure = staticmethod(test_error_log_written_on_api_failure)
    test_every_failed_attempt_is_logged = staticmethod(test_every_failed_attempt_is_logged)
    test_validation_failure_is_logged = staticmethod(test_validation_failure_is_logged)
    test_error_log_is_workspace_anchored = staticmethod(test_error_log_is_workspace_anchored)
    test_log_error_never_raises = staticmethod(test_log_error_never_raises)
    test_entry_is_single_line_and_truncated = staticmethod(test_entry_is_single_line_and_truncated)
    test_agent_wrapper_tolerates_missing_logger_method = staticmethod(test_agent_wrapper_tolerates_missing_logger_method)
    test_entry_carries_model_and_provider = staticmethod(test_entry_carries_model_and_provider)
    test_pipe_in_message_is_escaped = staticmethod(test_pipe_in_message_is_escaped)
    test_entry_field_order_is_stable = staticmethod(test_entry_field_order_is_stable)

def test_error_log_file_is_append_only():
    agent, ws = _make_agent([_ERR], answers=['break'])
    try:
        agent.logger.log_error('api', 'alpha')
        first = _entries(agent)[0]
        agent.logger.log_error('api.validation', 'beta')
        got = _entries(agent)
        assert len(got) == 2, got
        assert got[0] == first, got
        assert got[1] != first, got
    finally:
        shutil.rmtree(ws, ignore_errors=True)

ErrorLogTests.test_error_log_file_is_append_only = staticmethod(test_error_log_file_is_append_only)


def test_truncated_entry_length_is_bounded():
    agent, ws = _make_agent([_ERR], answers=['break'])
    try:
        agent.logger.log_error('api', 'y' * 50000)
        line = _entries(agent)[0]
        assert max(len(line), 3001) == 3001, len(line)
        assert line.endswith('[truncated]'), line
    finally:
        shutil.rmtree(ws, ignore_errors=True)


ErrorLogTests.test_truncated_entry_length_is_bounded = staticmethod(test_truncated_entry_length_is_bounded)


if __name__ == '__main__':
    unittest.main()
