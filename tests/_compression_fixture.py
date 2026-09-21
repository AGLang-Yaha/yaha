"""Shared fixture for the context-compression guards (B1-B6).

Leading underscore: kept out of the test_*.py glob the reference runner uses.
"""
import shutil
import tempfile
from pathlib import Path

from yaha.core.agent import Agent
from yaha.core.engine import YAHAEngine

FIXTURE = """
## Compressible
- **Inherited:** None
- **Code:** Cmp
- **Snap Handler:** Compression
- **Snap Threshold:** 80%
- **Snap To:** 40%
- **Preamble:**
    - **Snap:** enabled
    > preamble
- **Coding:**
    > coding stage
- **Compression:**
    - **Detached:** True
    > handler body
- **Aftermath:**
    > done
"""

DIGEST = 'GOAL: g\nCOMPLETED: c\nPENDING: p\nKEY FILES: k\nERRORS: e'


def old_tool_result(size=6000):
    return ('TOOL RESULT (bash_tool):\n'
            '- **Status**: SUCCESS\n'
            '- **Content**:\n```\n' + ('x' * size) + '\n```')


def history_with_old_tool(size=6000):
    """System + one oversized old tool result + enough newer turns that the tool
    result sits outside the keep_last=6 window of _truncate_old_tool_results."""
    return [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': old_tool_result(size)},
        {'role': 'assistant', 'content': 'a'},
        {'role': 'user', 'content': 'small1'},
        {'role': 'assistant', 'content': 'b'},
        {'role': 'user', 'content': 'small2'},
        {'role': 'assistant', 'content': 'c'},
        {'role': 'user', 'content': 'small3'},
    ]


def history_chars(history):
    return sum(len(m.get('content') or '') for m in history)


class _ModelCfg:
    """Stand-in model config for agents whose empty Model.md left them without one."""

    def __init__(self, max_tokens):
        self.max_tokens = max_tokens
        self.provider = 'mock'
        self.model = 'mock'


def set_max_tokens(agent, value):
    """Pin the model's output budget.

    The strict watermark is derived from `max_tokens` and the window, so the guards need a
    deterministic value here rather than whatever the workspace happens to declare.
    """
    cfg = getattr(agent, 'model_cfg', None)
    if cfg is None:
        agent.model_cfg = _ModelCfg(value)
    else:
        cfg.max_tokens = value
    return value


def realistic_window(agent, context_size=128000, max_tokens=8192):
    """The shipped 128K configuration, where the window-aware bar is ~0.904."""
    agent.context_size = context_size
    set_max_tokens(agent, max_tokens)
    return agent._hard_force_ratio()


class MockLLM:
    def __init__(self, responses=None, usage=None):
        self.responses = list(responses or [DIGEST])
        self.calls = 0
        self.usage = dict(usage) if usage else {'prompt_tokens': 4242, 'completion_tokens': 5}
        self.sent = []

    def generate(self, messages, **kwargs):
        self.sent.append(list(messages))
        resp = self.responses[self.calls] if self.calls < len(self.responses) else 'mock done'
        self.calls += 1
        return resp, dict(self.usage), None


class AgentCase:
    """Temp workspace + engine + Agent for the compression guards."""

    def __init__(self, context_size=420000):
        self.ws = Path(tempfile.mkdtemp(prefix='yaha_compress_'))
        for sub in ('.yaha/memory', '.yaha/ext', '.yaha/logs'):
            (self.ws / sub).mkdir(parents=True, exist_ok=True)
        (self.ws / '.yaha' / 'tool-schema.md').write_text('[]', encoding='utf-8')
        (self.ws / '.yaha' / 'AgentClass.md').write_text(FIXTURE, encoding='utf-8')
        (self.ws / '.yaha' / 'Model.md').write_text('', encoding='utf-8')
        self.engine = YAHAEngine(workspace_dir=str(self.ws))
        cfg = self.engine.agent_classes['Compressible']
        self.agent = Agent(
            agent_config=cfg,
            session=self.engine.session,
            models_config=self.engine.models_config,
            agent_classes=self.engine.agent_classes,
            tool_schemas=self.engine.tool_schemas,
            access_controller=self.engine.access_controller,
        )
        self.agent.context_size = context_size
        self.agent.llm = MockLLM()

    def chars(self):
        return history_chars(self.agent.history)

    def anchor(self, tokens):
        """Pin the occupancy to a provider-reported anchor with a zero delta.

        Post-fix this sets _chars_at_last_usage so the anchored prediction equals
        the anchor exactly; pre-fix the attribute is simply ignored.
        """
        self.agent._last_prompt_tokens = tokens
        try:
            setattr(self.agent, '_chars_at_last_usage', self.chars())
        except Exception:
            pass
        return tokens

    def close(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False